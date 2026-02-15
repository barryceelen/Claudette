import sublime
import json
import urllib.request
import urllib.parse
import urllib.error
import ssl
from typing import Any
from ..statusbar.spinner import ClaudetteSpinner
from ..constants import ANTHROPIC_VERSION, DEFAULT_MODEL, DEFAULT_BASE_URL, MAX_TOKENS, SETTINGS_FILE, DEFAULT_VERIFY_SSL
from ..utils import claudette_get_api_key_value

# Valid effort values for adaptive thinking (Opus 4.6). Default "high" if omitted.
VALID_EFFORT = frozenset(('low', 'medium', 'high', 'max'))

# Minimum thinking budget for manual mode; budget must be < max_tokens.
THINKING_BUDGET_MIN = 1024


def _is_thinking_related_400(error_body_str):
    """Return True if the API error indicates thinking/adaptive/output_config is invalid."""
    try:
        data = json.loads(error_body_str)
        error = data.get('error') or {}
        msg = (error.get('message') or '').lower()
        typ = (error.get('type') or '').lower()
        for token in ('thinking', 'adaptive', 'budget_tokens', 'effort', 'output_config'):
            if token in msg or token in typ:
                return True
        return False
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False


def _apply_thinking_mode(data, mode, thinking_effort, thinking_budget_tokens, max_tokens):
    """Set thinking and optional output_config on data by mode: 'adaptive', 'manual', or 'none'."""
    if mode is None or mode == 'none':
        return
    if mode == 'adaptive':
        data['thinking'] = {'type': 'adaptive'}
        data['output_config'] = {'effort': thinking_effort}
    elif mode == 'manual':
        budget = max(THINKING_BUDGET_MIN, min(thinking_budget_tokens, max_tokens - 1))
        data['thinking'] = {'type': 'enabled', 'budget_tokens': budget}


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
        # Thinking mode settings
        thinking_settings = self.settings.get('thinking', {})
        self.thinking_enabled = thinking_settings.get('enabled', False)
        self.thinking_budget_tokens = thinking_settings.get('budget_tokens', 10000)
        effort = thinking_settings.get('effort', 'high')
        self.thinking_effort = effort if effort in VALID_EFFORT else 'high'

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

    def stream_response(self, chunk_callback, messages, chat_view=None):
        input_tokens = 0
        output_tokens = 0
        cache_info = ""

        def handle_error(error_msg):
            sublime.set_timeout(
                lambda: chunk_callback(error_msg, is_done=True, is_thinking=False),
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

            # Filter messages, handling both string content and content arrays (for thinking)
            filtered_messages = []
            for msg in messages:
                content = msg.get('content', '')
                # Handle string content
                if isinstance(content, str) and content.strip():
                    filtered_messages.append(msg)
                # Handle content array (for thinking responses)
                elif isinstance(content, list) and len(content) > 0:
                    # Preserve order; keep thinking (with signature), redacted_thinking (with data), and other blocks
                    sanitized = []
                    for block in content:
                        if not isinstance(block, dict):
                            sanitized.append(block)
                            continue
                        if block.get('type') == 'thinking':
                            if block.get('signature'):
                                sanitized.append(block)
                        elif block.get('type') == 'redacted_thinking':
                            if block.get('data') is not None:
                                sanitized.append(block)
                        else:
                            sanitized.append(block)
                    if sanitized:
                        filtered_messages.append({'role': msg['role'], 'content': sanitized})

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
                        system_message: dict[str, Any] = {
                            "type": "text",
                            "text": combined_content
                        }

                        system_message['cache_control'] = {"type": "ephemeral"}
                        system_messages.append(system_message)

            # When thinking is enabled, temperature must be 1.0
            temperature = 1.0 if self.thinking_enabled else self.get_valid_temperature(self.temperature)

            base_data = {
                'messages': filtered_messages,
                'max_tokens': self.max_tokens,
                'model': self.model,
                'stream': True,
                'system': system_messages,
                'temperature': temperature
            }

            # Try-and-fallback for thinking: adaptive -> manual -> none (we do not want to use a hardcoded list of models thqt support thinking)
            if not self.thinking_enabled:
                modes_to_try = [None]
            else:
                cache = {}
                if chat_view and hasattr(chat_view, 'settings'):
                    cache = dict(chat_view.settings().get('claudette_thinking_mode_cache', {}))
                cached_mode = cache.get(self.model)
                modes_to_try = [cached_mode] if cached_mode is not None else ['adaptive', 'manual', 'none']

            ssl_context = self._get_ssl_context()
            stream_done = False

            for mode in modes_to_try:
                data = dict(base_data)
                if self.thinking_enabled:
                    _apply_thinking_mode(
                        data, mode,
                        self.thinking_effort,
                        self.thinking_budget_tokens,
                        self.max_tokens
                    )

                req = urllib.request.Request(
                    urllib.parse.urljoin(self.base_url, 'messages'),
                    data=json.dumps(data).encode('utf-8'),
                    headers=headers,
                    method='POST'
                )

                try:
                    response = urllib.request.urlopen(req, context=ssl_context)
                except urllib.error.HTTPError as e:
                    if e.code != 400:
                        error_content = e.read().decode('utf-8')
                        try:
                            error_data = json.loads(error_content)
                            error_message = error_data.get('error', {}).get('message', '')
                        except (json.JSONDecodeError, AttributeError, KeyError):
                            error_message = ''
                        if error_message:
                            handle_error("[Error] {0}".format(error_message))
                        else:
                            handle_error("[Error] {0}".format(str(e)))
                        stream_done = True
                        break
                    error_content = e.read().decode('utf-8')
                    if _is_thinking_related_400(error_content) and mode != 'none':
                        continue  # Retry with next mode
                    # Non-thinking 400 or last mode failed: show error
                    try:
                        error_data = json.loads(error_content)
                        error_message = error_data.get('error', {}).get('message', str(e))
                    except (json.JSONDecodeError, AttributeError, KeyError):
                        error_message = str(e)
                    handle_error("[Error] {0}".format(error_message))
                    stream_done = True
                    break
                except urllib.error.URLError as e:
                    handle_error(f"[Error] {str(e)}")
                    stream_done = True
                    break

                # Success: consume stream
                thinking_used = self.thinking_enabled and mode in ('adaptive', 'manual')
                current_block_type = None
                thinking_started = False

                try:
                    with response:
                        for line in response:
                            if not line or line.isspace():
                                continue

                            try:
                                chunk = line.decode('utf-8')
                                if not chunk.startswith('data: '):
                                    continue

                                chunk = chunk[6:]  # Remove 'data: ' prefix
                                if chunk.strip() == '[DONE]':
                                    break

                                event = json.loads(chunk)

                                # Get initial input tokens from message_start
                                if event.get('type') == 'message_start':
                                    if 'message' in event and 'usage' in event['message']:
                                        usage = event['message']['usage']
                                        input_tokens = usage.get('input_tokens', 0)
                                        cache_read_tokens = usage.get('cache_read_input_tokens', 0)
                                        cache_write_tokens = usage.get('cache_write_input_tokens', 0)
                                        if cache_read_tokens > 0:
                                            cache_info = f" (cache read: {cache_read_tokens:,})"
                                        elif cache_write_tokens > 0:
                                            cache_info = f" (cache write: {cache_write_tokens:,})"

                                # Handle content block start - thinking, redacted_thinking, or text
                                if event.get('type') == 'content_block_start':
                                    block = event.get('content_block', {})
                                    current_block_type = block.get('type')

                                    if current_block_type == 'thinking':
                                        thinking_started = True
                                        sublime.set_timeout(
                                            lambda: chunk_callback("", is_done=False, is_thinking=True, thinking_event='start'),
                                            0
                                        )
                                    elif current_block_type == 'redacted_thinking':
                                        thinking_started = True
                                        sublime.set_timeout(
                                            lambda: chunk_callback(
                                                "", is_done=False, is_thinking=False,
                                                thinking_event='start_redacted'
                                            ),
                                            0
                                        )
                                    elif current_block_type == 'text' and thinking_started:
                                        sublime.set_timeout(
                                            lambda: chunk_callback("", is_done=False, is_thinking=True, thinking_event='end'),
                                            0
                                        )
                                        thinking_started = False

                                # Handle content block stop - close redacted_thinking block
                                if event.get('type') == 'content_block_stop' and current_block_type == 'redacted_thinking':
                                    sublime.set_timeout(
                                        lambda: chunk_callback(
                                            "", is_done=False, is_thinking=False,
                                            thinking_event='end_redacted'
                                        ),
                                        0
                                    )
                                    current_block_type = None

                                # Handle content deltas - thinking, signature, redacted data, text
                                if 'delta' in event:
                                    delta = event['delta']
                                    if delta.get('type') == 'signature_delta' and 'signature' in delta:
                                        sig = delta['signature']
                                        sublime.set_timeout(
                                            lambda s=sig: chunk_callback("", is_done=False, is_thinking=False, thinking_signature=s),
                                            0
                                        )
                                    elif 'thinking' in delta:
                                        sublime.set_timeout(
                                            lambda text=delta['thinking']: chunk_callback(text, is_done=False, is_thinking=True),
                                            0
                                        )
                                    elif 'data' in delta:
                                        sublime.set_timeout(
                                            lambda d=delta['data']: chunk_callback(
                                                "", is_done=False, is_thinking=False,
                                                redacted_data=d
                                            ),
                                            0
                                        )
                                    elif 'text' in delta:
                                        sublime.set_timeout(
                                            lambda text=delta['text']: chunk_callback(text, is_done=False, is_thinking=False),
                                            0
                                        )

                                # Get final output tokens from message_delta
                                if event.get('type') == 'message_delta' and 'usage' in event:
                                    output_tokens = event['usage'].get('output_tokens', 0)

                                # Send token information at the end
                                if event.get('type') == 'message_stop':
                                    # Get cache token information
                                    cache_read_tokens = event.get('usage', {}).get('cache_read_input_tokens', 0)
                                    cache_write_tokens = event.get('usage', {}).get('cache_write_input_tokens', 0)

                                    # Calculate current response cost including cache operations
                                    current_cost = self.calculate_cost(
                                        input_tokens,
                                        output_tokens,
                                        cache_read_tokens=cache_read_tokens,
                                        cache_write_tokens=cache_write_tokens
                                    )

                                    # Format current cost
                                    current_cost_str = f"${current_cost:.4f}"

                                    # Update chat view's session stats
                                    session_stats = None
                                    if chat_view and hasattr(chat_view, 'settings'):
                                        settings = chat_view.settings()

                                        # Get current session stats from settings
                                        session_stats = settings.get('claudette_session_stats', {
                                            'input_tokens': 0,
                                            'output_tokens': 0,
                                            'cost': 0.0
                                        })

                                        # Update session totals
                                        session_stats['input_tokens'] += input_tokens
                                        session_stats['output_tokens'] += output_tokens
                                        session_stats['cost'] += current_cost

                                        # Save updated stats back to settings
                                        settings.set('claudette_session_stats', session_stats)

                                        session_cost_str = f"${session_stats['cost']:.4f}"
                                    else:
                                        session_cost_str = f"${current_cost:.4f}"

                                    # Build status message with thinking indicator if actually used
                                    thinking_info = " (incl. thinking)" if thinking_used else ""
                                    status_message = f"Tokens: {input_tokens:,} sent, {output_tokens:,} received{thinking_info}{cache_info}."

                                    if session_stats and session_stats.get('cost', 0) > 0:
                                        status_message_cost = f" Cost: {current_cost_str} message, {session_cost_str} session."
                                        status_message = status_message + status_message_cost

                                    # Schedule status message on main thread with a delay
                                    def show_delayed_status():
                                        sublime.status_message(status_message)

                                    sublime.set_timeout(show_delayed_status, 100)

                                    # Signal completion
                                    sublime.set_timeout(
                                        lambda: chunk_callback("", is_done=True, is_thinking=False),
                                        0
                                    )

                            except Exception:
                                continue

                except Exception:
                    pass
                # Cache working mode for this model so next request skips failed attempts
                if chat_view and hasattr(chat_view, 'settings') and mode is not None:
                    cache = dict(chat_view.settings().get('claudette_thinking_mode_cache', {}))
                    cache[self.model] = mode
                    chat_view.settings().set('claudette_thinking_mode_cache', cache)
                if mode == 'none' and self.thinking_enabled:
                    sublime.set_timeout(
                        lambda: sublime.status_message('Thinking not supported for this model; response without thinking.'),
                        150
                    )
                stream_done = True
                break

            if not stream_done:
                pass  # Error already reported in loop
            try:
                pass
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
