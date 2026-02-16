import sublime
import json
import urllib.request
import urllib.parse
import urllib.error
import ssl
from typing import Any
from ..statusbar.spinner import ClaudetteSpinner
from ..constants import ANTHROPIC_VERSION, DEFAULT_MODEL, DEFAULT_BASE_URL, MAX_TOKENS, SETTINGS_FILE, DEFAULT_VERIFY_SSL
from ..utils import claudette_get_api_key_value, claudette_chat_status_message
from ..tools.text_editor import run_text_editor_tool

class ClaudetteClaudeAPI:
    def __init__(self):
        self.settings = sublime.load_settings(SETTINGS_FILE)
        self.api_key = claudette_get_api_key_value()
        self.base_url = self.settings.get('base_url', DEFAULT_BASE_URL)
        try:
            self.max_tokens = int(self.settings.get('max_tokens', MAX_TOKENS))
        except (TypeError, ValueError):
            self.max_tokens = MAX_TOKENS
        self.model = self.settings.get('model', DEFAULT_MODEL)
        self.temperature = self.settings.get('temperature', '1.0')
        self.session_cost = 0.0
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.spinner = ClaudetteSpinner()
        self.pricing = self.settings.get('pricing')
        self.verify_ssl = self.settings.get('verify_ssl', DEFAULT_VERIFY_SSL)

    def _get_ssl_context(self):
        """Create and return an SSL context based on verify_ssl setting."""
        if self.verify_ssl:
            # Use default SSL context with verification enabled
            return ssl.create_default_context()
        else:
            # Create unverified SSL context for self-signed certificates
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            return ssl_context

    @staticmethod
    def get_valid_temperature(temp):
        try:
            temp = float(temp)
            if 0.0 <= temp <= 1.0:
                return temp
            return 1.0
        except (TypeError, ValueError):
            return 1.0

    def calculate_cost(self, input_tokens, output_tokens, cache_read_tokens=0, cache_write_tokens=0, model=None):
        """Calculate cost based on token usage and model.

        Args:
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            cache_read_tokens: Number of tokens read from cache
            cache_write_tokens: Number of tokens written to cache
            model: Model name (optional, defaults to current model)
        """
        if model is None:
            model = self.model

        price_tier = None
        model_lower = model.lower()

        for tier in self.pricing.keys():
            if tier in model_lower:
                price_tier = self.pricing[tier]
                break

        if not price_tier:
            return 0

        input_cost = ((input_tokens - cache_read_tokens) / 1000) * price_tier['input']
        output_cost = (output_tokens / 1000) * price_tier['output']
        cache_write_cost = (cache_write_tokens / 1000) * price_tier.get('cache_write', 0)
        cache_read_cost = (cache_read_tokens / 1000) * price_tier.get('cache_read', 0)

        return input_cost + output_cost + cache_write_cost + cache_read_cost

    @staticmethod
    def _message_has_content(msg):
        """Return True if message has content to send (string or list for tool turns)."""
        content = msg.get('content')
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            return len(content) > 0
        return False

    def _get_text_editor_tool_def(self):
        """Return text editor tool definition for current model, or None if disabled."""
        if not self.settings.get('text_editor_tool', False):
            return None
        model_lower = (self.model or '').lower()
        if 'claude-3-7' in model_lower:
            tool_def = {
                'type': 'text_editor_20250124',
                'name': 'str_replace_editor',
            }
        else:
            tool_def = {
                'type': 'text_editor_20250728',
                'name': 'str_replace_based_edit_tool',
            }
            try:
                max_chars = int(self.settings.get('text_editor_tool_max_characters', 0))
                if max_chars > 0:
                    tool_def['max_characters'] = max_chars
            except (TypeError, ValueError):
                pass
        return tool_def

    def _build_system_messages(self, chat_view=None):
        """Build system messages list (with optional context files)."""
        system_messages = [
            {
                "type": "text",
                "text": '''Format responses in markdown. Do not add a summary before your answer. Wrap code in fenced code blocks.

If the reponse warrants being structured in sections, use this heading structure (h1 is reserved for the chat interface):

Content here.

## Subtopic

More content.

```python
# code example
```''',
            }
        ]

        settings_system_messages = self.settings.get('system_messages', [])
        default_index = self.settings.get('default_system_message_index', 0)

        if (settings_system_messages and
                isinstance(settings_system_messages, list) and
                isinstance(default_index, int) and
                0 <= default_index < len(settings_system_messages)):

            selected_message = settings_system_messages[default_index]
            if selected_message and selected_message.strip():
                system_messages.append({
                    "type": "text",
                    "text": selected_message.strip()
                })

        if chat_view:
            context_files = chat_view.settings().get('claudette_context_files', {})
            if context_files:
                combined_content = "<reference_files>\n"
                for file_path, file_info in context_files.items():
                    if file_info.get('content'):
                        combined_content += f"<file>\n"
                        combined_content += f"<path>{file_path}</path>\n"
                        combined_content += f"<content>\n{file_info['content']}\n</content>\n"
                        combined_content += "</file>\n"
                combined_content += "</reference_files>"

                if combined_content != "<reference_files>\n</reference_files>":
                    system_message = {
                        "type": "text",
                        "text": combined_content
                    }
                    system_message['cache_control'] = {"type": "ephemeral"}
                    system_messages.append(system_message)

        return system_messages

    def _request_non_streaming(self, messages, system_messages, tools_list=None):
        """
        Send a single non-streaming request. Returns (response_message_dict, usage_dict).
        response_message_dict has 'content' (list of blocks) and 'stop_reason'.
        """
        headers = {
            'x-api-key': self.api_key,
            'anthropic-version': ANTHROPIC_VERSION,
            'content-type': 'application/json',
        }

        data = {
            'messages': messages,
            'max_tokens': self.max_tokens,
            'model': self.model,
            'stream': False,
            'system': system_messages,
            'temperature': self.get_valid_temperature(self.temperature)
        }
        if tools_list:
            data['tools'] = tools_list

        req = urllib.request.Request(
            urllib.parse.urljoin(self.base_url, 'messages'),
            data=json.dumps(data).encode('utf-8'),
            headers=headers,
            method='POST'
        )

        ssl_context = self._get_ssl_context()
        with urllib.request.urlopen(req, context=ssl_context) as response:
            body = json.loads(response.read().decode('utf-8'))

        # Response may have message nested under 'message' or be the message at top level.
        msg = body.get('message') if body.get('message') is not None else body
        if not isinstance(msg, dict):
            msg = {}
        usage = body.get('usage') or msg.get('usage') or {}
        return msg, usage

    def run_with_text_editor_loop(self, chunk_callback, messages, chat_view, on_complete_cb=None):
        """
        Run request loop with text editor tool: non-streaming requests until end_turn.
        Calls chunk_callback with final text and on_complete_cb when done.
        """
        def handle_error(error_msg):
            sublime.set_timeout(
                lambda: chunk_callback(error_msg, is_done=True),
                0
            )

        filtered = [m for m in messages if self._message_has_content(m)]
        if not filtered:
            return

        if not self.api_key:
            handle_error("[Error] The API key is not set. Please check your API key configuration.")
            return

        text_editor_tool = self._get_text_editor_tool_def()
        if not text_editor_tool:
            handle_error("[Error] Text editor tool is not enabled.")
            return

        tools_list = [text_editor_tool]
        if self.settings.get('web_search', False):
            try:
                max_uses = int(self.settings.get('web_search_max_uses', 5))
                max_uses = max(1, min(20, max_uses))
            except (TypeError, ValueError):
                max_uses = 5
            tool_def = {
                'type': 'web_search_20250305',
                'name': 'web_search',
                'max_uses': max_uses
            }
            allowed = self.settings.get('web_search_allowed_domains')
            blocked = self.settings.get('web_search_blocked_domains')
            if allowed and isinstance(allowed, list) and len(allowed) > 0:
                tool_def['allowed_domains'] = [str(d).strip() for d in allowed if str(d).strip()]
            elif blocked and isinstance(blocked, list) and len(blocked) > 0:
                tool_def['blocked_domains'] = [str(d).strip() for d in blocked if str(d).strip()]
            user_loc = self.settings.get('web_search_user_location')
            if (user_loc and isinstance(user_loc, dict) and user_loc.get('type') == 'approximate' and
                    (user_loc.get('city') or user_loc.get('country') or user_loc.get('timezone'))):
                tool_def['user_location'] = {
                    'type': 'approximate',
                    'city': str(user_loc.get('city', '')),
                    'region': str(user_loc.get('region', '')),
                    'country': str(user_loc.get('country', '')),
                    'timezone': str(user_loc.get('timezone', ''))
                }
            tools_list.append(tool_def)

        # chat_view may be the sublime View or a ClaudetteChatView (has .view, .set_tool_status, .clear_tool_status).
        view_for_api = getattr(chat_view, 'view', chat_view)
        chat_view_for_status = chat_view if hasattr(chat_view, 'set_tool_status') else None

        system_messages = self._build_system_messages(view_for_api)
        window = view_for_api.window() if view_for_api else None
        settings = self.settings
        try:
            max_chars = int(self.settings.get('text_editor_tool_max_characters', 0))
        except (TypeError, ValueError):
            max_chars = None

        try:
            self.spinner.start('Fetching response')
            current_messages = list(filtered)

            while True:
                try:
                    msg, usage = self._request_non_streaming(current_messages, system_messages, tools_list)
                except urllib.error.HTTPError as e:
                    self.spinner.stop()
                    if chat_view_for_status:
                        sublime.set_timeout(lambda: chat_view_for_status.clear_tool_status(), 0)
                    error_content = e.read().decode('utf-8')
                    try:
                        err_data = json.loads(error_content)
                        error_message = err_data.get('error', {}).get('message', str(e))
                    except (json.JSONDecodeError, AttributeError, KeyError):
                        error_message = str(e)
                    handle_error("[Error] {0}".format(error_message))
                    return
                except urllib.error.URLError as e:
                    self.spinner.stop()
                    if chat_view_for_status:
                        sublime.set_timeout(lambda: chat_view_for_status.clear_tool_status(), 0)
                    handle_error("[Error] {0}".format(str(e)))
                    return

                stop_reason = (msg.get('stop_reason') or '').strip() or None
                content = msg.get('content') or []

                # If API omits stop_reason, treat as end_turn when we have text content.
                if not stop_reason and content:
                    has_text = any(
                        isinstance(b, dict) and b.get('type') == 'text'
                        for b in content
                    )
                    if has_text:
                        stop_reason = 'end_turn'

                if stop_reason == 'tool_use':
                    def update_status(label):
                        self.spinner.set_message(label)
                        if chat_view_for_status:
                            chat_view_for_status.set_tool_status(label)
                    sublime.set_timeout(
                        lambda: update_status('Reading/editing files…'),
                        0
                    )
                    tool_results = []
                    assistant_content = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get('type') == 'text' and block.get('text'):
                            assistant_content.append(block)
                        elif block.get('type') == 'tool_use':
                            assistant_content.append(block)
                            inp = block.get('input', {})
                            path = inp.get('path', '') or 'file'
                            cmd = inp.get('command', 'view')
                            action = {
                                'view': 'Reading',
                                'str_replace': 'Editing',
                                'create': 'Creating',
                                'insert': 'Editing',
                            }.get(cmd, 'Processing')
                            status_label = '{0} {1}'.format(action, path)
                            sublime.set_timeout(
                                lambda s=status_label: update_status(s),
                                0
                            )
                            context_files = None
                            if view_for_api and hasattr(view_for_api, 'settings'):
                                context_files = view_for_api.settings().get('claudette_context_files')
                            result = run_text_editor_tool(
                                block.get('id', ''),
                                block.get('name', ''),
                                inp,
                                window,
                                settings,
                                max_characters=max_chars,
                                context_files=context_files,
                            )
                            tool_results.append(result)

                    user_content = tool_results
                    current_messages.append({'role': 'assistant', 'content': assistant_content})
                    current_messages.append({'role': 'user', 'content': user_content})
                    continue

                if stop_reason == 'end_turn':
                    self.spinner.stop()
                    if chat_view_for_status:
                        sublime.set_timeout(lambda: chat_view_for_status.clear_tool_status(), 0)
                    text_parts = []
                    sources_lines = []
                    
                    # Process all content blocks (text and web search results)
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if isinstance(block, dict) and block.get('type') == 'text' and block.get('text'):
                            text_parts.append(block['text'])
                        elif block.get('type') == 'web_search_tool_result':
                            # Extract web search results
                            for item in block.get('content', []):
                                if not isinstance(item, dict):
                                    continue
                                if item.get('type') == 'web_search_result':
                                    url = item.get('url', '')
                                    title = item.get('title', url)
                                    if url:
                                        sources_lines.append("- [{0}]({1})".format(title, url))
                    
                    # Display web search results first (directly under # Claude's Response)
                    final_text = ''.join(text_parts)
                    if sources_lines:
                        sources_text = "### Search Results\n\n" + "\n".join(sources_lines) + "\n\n"
                        sublime.set_timeout(
                            lambda t=sources_text: chunk_callback(t, is_done=False),
                            0
                        )
                    # Display text content
                    if final_text:
                        sublime.set_timeout(
                            lambda t=final_text: chunk_callback(t, is_done=False),
                            0
                        )
                    input_tokens = usage.get('input_tokens', 0)
                    output_tokens = usage.get('output_tokens', 0)
                    if view_for_api and hasattr(view_for_api, 'settings'):
                        sess = view_for_api.settings().get('claudette_session_stats', {
                            'input_tokens': 0, 'output_tokens': 0, 'cost': 0.0, 'web_search_requests': 0
                        })
                        if 'web_search_requests' not in sess:
                            sess['web_search_requests'] = 0
                        sess['input_tokens'] = sess.get('input_tokens', 0) + input_tokens
                        sess['output_tokens'] = sess.get('output_tokens', 0) + output_tokens
                        sess['cost'] = sess.get('cost', 0.0) + self.calculate_cost(input_tokens, output_tokens)
                        view_for_api.settings().set('claudette_session_stats', sess)
                        cache_info = ""
                        if usage.get('cache_read_input_tokens'):
                            cache_info = " (cache read: {0:,})".format(usage.get('cache_read_input_tokens', 0))
                        elif usage.get('cache_write_input_tokens'):
                            cache_info = " (cache write: {0:,})".format(usage.get('cache_write_input_tokens', 0))
                        status_message = "Tokens: {0:,} sent, {1:,} received{2}.".format(
                            input_tokens, output_tokens, cache_info
                        )
                        if sess.get('cost', 0) > 0:
                            status_message += " Cost: ${0:.4f} message, ${1:.4f} session.".format(
                                self.calculate_cost(input_tokens, output_tokens), sess['cost']
                            )
                        sublime.set_timeout(lambda s=status_message: sublime.status_message(s), 100)
                    sublime.set_timeout(
                        lambda: chunk_callback("", is_done=True),
                        0
                    )
                    return

                self.spinner.stop()
                if chat_view_for_status:
                    sublime.set_timeout(lambda: chat_view_for_status.clear_tool_status(), 0)
                handle_error(
                    "[Error] Unexpected stop_reason: {0}. "
                    "The API response may have a different structure.".format(
                        repr(stop_reason) if stop_reason else "(empty)"
                    )
                )
                return

        except Exception as e:
            self.spinner.stop()
            if chat_view_for_status:
                sublime.set_timeout(lambda: chat_view_for_status.clear_tool_status(), 0)
            handle_error("[Error] {0}".format(str(e)))

    def stream_response(self, chunk_callback, messages, chat_view=None):
        input_tokens = 0
        output_tokens = 0
        web_search_requests = 0
        cache_info = ""

        def handle_error(error_msg):
            sublime.set_timeout(
                lambda: chunk_callback(error_msg, is_done=True),
                0
            )

        if not messages or not any(msg.get('content', '').strip() for msg in messages):
            return

        if not self.api_key:
            handle_error(f"[Error] The API key is not set. Please check your API key configuration.")
            return

        try:
            self.spinner.start('Fetching response')

            headers = {
                'x-api-key': self.api_key,
                'anthropic-version': ANTHROPIC_VERSION,
                'content-type': 'application/json',
            }

            filtered_messages = [
                msg for msg in messages
                if self._message_has_content(msg)
            ]

            system_messages = self._build_system_messages(chat_view)

            data = {
                'messages': filtered_messages,
                'max_tokens': self.max_tokens,
                'model': self.model,
                'stream': True,
                'system': system_messages,
                'temperature': self.get_valid_temperature(self.temperature)
            }

            if self.settings.get('web_search', False):
                try:
                    max_uses = int(self.settings.get('web_search_max_uses', 5))
                    max_uses = max(1, min(20, max_uses))
                except (TypeError, ValueError):
                    max_uses = 5
                tool_def = {
                    'type': 'web_search_20250305',
                    'name': 'web_search',
                    'max_uses': max_uses
                }
                allowed = self.settings.get('web_search_allowed_domains')
                blocked = self.settings.get('web_search_blocked_domains')
                if allowed and isinstance(allowed, list) and len(allowed) > 0:
                    tool_def['allowed_domains'] = [str(d).strip() for d in allowed if str(d).strip()]
                elif blocked and isinstance(blocked, list) and len(blocked) > 0:
                    tool_def['blocked_domains'] = [str(d).strip() for d in blocked if str(d).strip()]
                user_loc = self.settings.get('web_search_user_location')
                if (user_loc and isinstance(user_loc, dict) and user_loc.get('type') == 'approximate' and
                        (user_loc.get('city') or user_loc.get('country') or user_loc.get('timezone'))):
                    tool_def['user_location'] = {
                        'type': 'approximate',
                        'city': str(user_loc.get('city', '')),
                        'region': str(user_loc.get('region', '')),
                        'country': str(user_loc.get('country', '')),
                        'timezone': str(user_loc.get('timezone', ''))
                    }
                data['tools'] = [tool_def]

            req = urllib.request.Request(
                urllib.parse.urljoin(self.base_url, 'messages'),
                data=json.dumps(data).encode('utf-8'),
                headers=headers,
                method='POST'
            )

            try:
                ssl_context = self._get_ssl_context()
                stream_web_search_sources = []
                stream_current_block_type = None
                stream_current_block_index = None

                def parse_web_search_items(items):
                    lines = []
                    has_error = False
                    for item in (items or []):
                        if not isinstance(item, dict):
                            continue
                        if item.get('type') == 'web_search_tool_result_error':
                            has_error = True
                            break
                        if item.get('type') == 'web_search_result':
                            url = item.get('url', '')
                            title = item.get('title', url)
                            if url:
                                lines.append("- [{0}]({1})".format(title, url))
                    return lines, has_error

                with urllib.request.urlopen(req, context=ssl_context) as response:
                    for line in response:
                        if not line or line.isspace():
                            continue

                        try:
                            chunk = line.decode('utf-8')
                            if not chunk.startswith('data: '):
                                continue

                            chunk = chunk[6:] # Remove 'data: ' prefix
                            if chunk.strip() == '[DONE]':
                                break

                            data = json.loads(chunk)

                            # Get initial input tokens from message_start
                            if data.get('type') == 'message_start':
                                if 'message' in data and 'usage' in data['message']:
                                    usage = data['message']['usage']
                                    input_tokens = usage.get('input_tokens', 0)
                                    cache_read_tokens = usage.get('cache_read_input_tokens', 0)
                                    cache_write_tokens = usage.get('cache_write_input_tokens', 0)
                                    if cache_read_tokens > 0:
                                        cache_info = f" (cache read: {cache_read_tokens:,})"
                                    elif cache_write_tokens > 0:
                                        cache_info = f" (cache write: {cache_write_tokens:,})"

                            # Web search: track block and accumulate sources from start/delta/stop
                            if data.get('type') == 'content_block_start':
                                content_block = data.get('content_block', {})
                                block_type = content_block.get('type')
                                idx = data.get('index')
                                stream_current_block_index = idx
                                stream_current_block_type = block_type

                                if block_type == 'server_tool_use' and content_block.get('name') == 'web_search':
                                    sublime.set_timeout(
                                        lambda: sublime.status_message('Searching the web...'),
                                        0
                                    )
                                elif block_type == 'web_search_tool_result':
                                    stream_web_search_sources = []
                                    sources_lines, has_error = parse_web_search_items(
                                        content_block.get('content', [])
                                    )
                                    if has_error:
                                        err_item = next(
                                            (it for it in (content_block.get('content') or [])
                                            if isinstance(it, dict)
                                            and it.get('type') == 'web_search_tool_result_error'
                                        ),
                                        None
                                        )
                                        error_code = err_item.get('error_code', 'unavailable') if err_item else 'unavailable'
                                        err_msg = "Web search error: {0}".format(error_code)
                                        view_ref = chat_view

                                        def show_web_search_error(msg=err_msg, v=view_ref):
                                            if v and v.window():
                                                claudette_chat_status_message(v.window(), msg, "⚠️")
                                            sublime.status_message(msg)

                                        sublime.set_timeout(show_web_search_error, 0)
                                    else:
                                        stream_web_search_sources.extend(sources_lines)
                                        sublime.set_timeout(lambda: sublime.status_message(''), 0)

                            elif data.get('type') == 'content_block_delta':
                                delta = data.get('delta', {})
                                idx = data.get('index')
                                if (
                                    idx == stream_current_block_index
                                    and stream_current_block_type == 'web_search_tool_result'
                                ):
                                    # Accumulate content from delta (API may send results incrementally)
                                    items = delta.get('content')
                                    if isinstance(items, list):
                                        sources_lines, has_error = parse_web_search_items(items)
                                        if not has_error:
                                            stream_web_search_sources.extend(sources_lines)
                                    elif isinstance(items, dict):
                                        sources_lines, has_error = parse_web_search_items([items])
                                        if not has_error:
                                            stream_web_search_sources.extend(sources_lines)

                            elif data.get('type') == 'content_block_stop':
                                idx = data.get('index')
                                if (
                                    idx == stream_current_block_index
                                    and stream_current_block_type == 'web_search_tool_result'
                                    and stream_web_search_sources
                                ):
                                    sources_text = "### Search Results\n\n" + "\n".join(
                                        stream_web_search_sources
                                    ) + "\n\n"
                                    sublime.set_timeout(
                                        lambda cb=chunk_callback, text=sources_text: cb(
                                            text, is_done=False, insert_after_response_header=True
                                        ),
                                        0
                                    )
                                if idx == stream_current_block_index:
                                    stream_current_block_type = None
                                    stream_current_block_index = None

                            # Handle content updates (text and optional citations)
                            if 'delta' in data:
                                delta = data['delta']
                                text = delta.get('text')
                                if text and (delta.get('type') == 'text_delta' or 'type' not in delta):
                                    sublime.set_timeout(
                                        lambda t=text: chunk_callback(t, is_done=False),
                                        0
                                    )
                                # Render citations as links when the API sends them (e.g. web search).
                                citations = delta.get('citations') if isinstance(delta.get('citations'), list) else []
                                for cit in citations:
                                    if isinstance(cit, dict):
                                        url = cit.get('url') or ''
                                        title = cit.get('title') or url or 'Source'
                                        if url:
                                            link_md = " [{0}]({1}) ".format(title, url)
                                            sublime.set_timeout(
                                                lambda cb=chunk_callback, md=link_md: cb(md, is_done=False),
                                                0
                                            )

                            # Get final output tokens from message_delta
                            if data.get('type') == 'message_delta' and 'usage' in data:
                                output_tokens = data['usage'].get('output_tokens', 0)

                            # Send token information at the end
                            if data.get('type') == 'message_stop':
                                # Get cache token information
                                usage = data.get('usage', {})
                                cache_read_tokens = usage.get('cache_read_input_tokens', 0)
                                cache_write_tokens = usage.get('cache_write_input_tokens', 0)
                                server_tool_use = usage.get('server_tool_use', {})
                                web_search_requests = server_tool_use.get('web_search_requests', 0)

                                # Calculate current response cost including cache and web search
                                current_cost = self.calculate_cost(
                                    input_tokens,
                                    output_tokens,
                                    cache_read_tokens=cache_read_tokens,
                                    cache_write_tokens=cache_write_tokens
                                )
                                web_search_cost = web_search_requests * (10.0 / 1000)
                                current_cost += web_search_cost

                                # Format current cost
                                current_cost_str = f"${current_cost:.4f}"

                                # Update chat view's session stats
                                if chat_view and hasattr(chat_view, 'settings'):
                                    settings = chat_view.settings()

                                    # Get current session stats from settings
                                    session_stats = settings.get('claudette_session_stats', {
                                        'input_tokens': 0,
                                        'output_tokens': 0,
                                        'cost': 0.0,
                                        'web_search_requests': 0
                                    })
                                    if 'web_search_requests' not in session_stats:
                                        session_stats['web_search_requests'] = 0

                                    # Update session totals
                                    session_stats['input_tokens'] += input_tokens
                                    session_stats['output_tokens'] += output_tokens
                                    session_stats['web_search_requests'] += web_search_requests
                                    session_stats['cost'] += current_cost

                                    # Save updated stats back to settings
                                    settings.set('claudette_session_stats', session_stats)

                                    session_cost_str = f"${session_stats['cost']:.4f}"
                                else:
                                    session_cost_str = f"${current_cost:.4f}"

                                status_message = f"Tokens: {input_tokens:,} sent, {output_tokens:,} received{cache_info}."

                                if session_stats['cost'] > 0:
                                    status_message_cost = f" Cost: {current_cost_str} message, {session_cost_str} session."
                                    status_message = status_message + status_message_cost

                                # Schedule status message on main thread with a delay
                                def show_delayed_status():
                                    sublime.status_message(status_message)

                                sublime.set_timeout(show_delayed_status, 100)

                                # Signal completion
                                sublime.set_timeout(
                                    lambda: chunk_callback("", is_done=True),
                                    0
                                )

                        except Exception:
                            continue # Skip invalid chunks without error messages

            except urllib.error.HTTPError as e:
                error_content = e.read().decode('utf-8')
                print("Claude API Error Content:", error_content)

                # Try to parse the API error message from the response body
                error_type = ''
                error_message = ''
                try:
                    error_data = json.loads(error_content)
                    error_type = error_data.get('error', {}).get('type', '')
                    error_message = error_data.get('error', {}).get('message', '')
                except (json.JSONDecodeError, AttributeError, KeyError):
                    pass

                # Check if it's a 404 model-not-found error
                if e.code == 404 and error_type == 'not_found_error' and error_message.startswith('model:'):
                    # Extract the model name from the error message
                    # Format: "model: claude-sonnet-4-5-latest"
                    error_model = error_message.replace('model:', '').strip()

                    display_message = f'The "{error_model}" model does not exist.'

                    # Get window from chat_view (which is a sublime.View)
                    window = None
                    if chat_view:
                        window = chat_view.window()

                    if window:
                        from ..utils import claudette_chat_status_message
                        from ..chat.chat_view import ClaudetteChatView

                        # Display the error message and get the end position
                        message_end_position = claudette_chat_status_message(
                            window,
                            display_message,
                            "⚠️"
                        )

                        # Add a button to open the select model panel
                        if message_end_position >= 0:
                            try:
                                chat_view_instance = ClaudetteChatView.get_instance(window, self.settings)
                                if chat_view_instance:
                                    chat_view_instance.add_select_model_button(message_end_position)
                            except Exception as e:
                                print(f"Error adding select model button: {str(e)}")
                    else:
                        # Fallback to chunk_callback if window not available
                        handle_error(f'[Error] {display_message} Please update your model via Settings > Package Settings > Claudette > Select Model.')
                elif error_message:
                    handle_error("[Error] {0}".format(error_message))
                else:
                    handle_error("[Error] {0}".format(str(e)))
            except urllib.error.URLError as e:
                handle_error(f"[Error] {str(e)}")
            finally:
                self.spinner.stop()

        except Exception as e:
            handle_error(f"[Error] {str(e)}")
            self.spinner.stop()

    def fetch_models(self):

        if not self.api_key:
            sublime.error_message(f"The API key is undefined. Please check your API key configuration.")
            return []

        try:
            sublime.status_message('Fetching models')
            headers = {
                'x-api-key': self.api_key,
                'anthropic-version': ANTHROPIC_VERSION,
            }

            req = urllib.request.Request(
                urllib.parse.urljoin(self.base_url, 'models'),
                headers=headers,
                method='GET'
            )

            ssl_context = self._get_ssl_context()
            with urllib.request.urlopen(req, context=ssl_context) as response:
                data = json.loads(response.read().decode('utf-8'))
                model_ids = [item['id'] for item in data['data']]
                sublime.status_message('')
                return model_ids

        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("Claude API: {0}".format(str(e)))
                sublime.error_message("Authentication invalid when fetching the available models from the Claude API.")
            else:
                print("Claude API: {0}".format(str(e)))
                sublime.error_message("An error occurred fetching the available models from the Claude API.")
        except urllib.error.URLError as e:
            print("Claude API: {0}".format(str(e)))
            sublime.error_message("An error occurred fetching the available models from the Claude API.")
        except Exception as e:
            print("Claude API: {0}".format(str(e)))
            sublime.error_message("An error occurred fetching the available models from the Claude API.")
        finally:
            sublime.status_message('')

        return []
