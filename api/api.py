import sublime
import json
import urllib.request
import urllib.parse
import urllib.error
import ssl
from typing import Any
from ..statusbar.spinner import ClaudetteSpinner
from ..constants import ANTHROPIC_VERSION, CACHE_SUPPORTED_MODEL_PREFIXES, DEFAULT_MODEL, DEFAULT_BASE_URL, MAX_TOKENS, SETTINGS_FILE, DEFAULT_VERIFY_SSL
from ..utils import claudette_get_api_key_value

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
    def should_use_cache_control(model):
        """Determine if cache control should be used based on model."""
        if not model:
            return False
        return any(model.startswith(prefix) for prefix in CACHE_SUPPORTED_MODEL_PREFIXES)

    # Model-specific token limits
    MODEL_MAX_TOKENS = {
        'claude-3-opus': 4096,
        'claude-3.5-sonnet': 8192,
        'claude-3.5-haiku': 4096,
        'claude-3-opus': 32000,
        'claude-3-7-sonnet': 64000,
        'claude-sonnet-4': 64000,
        'claude-opus-4': 32000,
    }

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
            # Get model-specific token limit or default to 4096
            model_prefix = next((prefix for prefix in self.MODEL_MAX_TOKENS.keys()
                               if self.model.startswith(prefix)), None)
            max_tokens = min(
                int(self.max_tokens),
                self.MODEL_MAX_TOKENS.get(model_prefix, 4096)
            )

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
                    filtered_messages.append(msg)

            system_messages = [
                {
                    "type": "text",
                    "text": 'Wrap all code examples in a markdown code block. Ensure each code block is complete and self-contained.',
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

                        if self.should_use_cache_control(self.model):
                            system_message['cache_control'] = {"type": "ephemeral"}

                        system_messages.append(system_message)

            # When thinking is enabled, temperature must be 1.0
            temperature = 1.0 if self.thinking_enabled else self.get_valid_temperature(self.temperature)

            data = {
                'messages': filtered_messages,
                'max_tokens': max_tokens,
                'model': self.model,
                'stream': True,
                'system': system_messages,
                'temperature': temperature
            }

            # Add thinking parameter when enabled
            if self.thinking_enabled:
                data['thinking'] = {
                    'type': 'enabled',
                    'budget_tokens': self.thinking_budget_tokens
                }

            req = urllib.request.Request(
                urllib.parse.urljoin(self.base_url, 'messages'),
                data=json.dumps(data).encode('utf-8'),
                headers=headers,
                method='POST'
            )

            try:
                ssl_context = self._get_ssl_context()
                current_block_type = None  # Track current content block type ('thinking' or 'text')
                thinking_started = False  # Track if we've started a thinking block

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

                            # Handle content block start - identify thinking vs text blocks
                            if data.get('type') == 'content_block_start':
                                block = data.get('content_block', {})
                                current_block_type = block.get('type')  # 'thinking' or 'text'

                                # Start thinking section with header
                                if current_block_type == 'thinking' and not thinking_started:
                                    thinking_started = True
                                    sublime.set_timeout(
                                        lambda: chunk_callback("", is_done=False, is_thinking=True, thinking_event='start'),
                                        0
                                    )
                                # When text block starts after thinking, signal transition
                                elif current_block_type == 'text' and thinking_started:
                                    sublime.set_timeout(
                                        lambda: chunk_callback("", is_done=False, is_thinking=True, thinking_event='end'),
                                        0
                                    )

                            # Handle content deltas - both thinking and text
                            if 'delta' in data:
                                # Handle thinking deltas
                                if 'thinking' in data['delta']:
                                    sublime.set_timeout(
                                        lambda text=data['delta']['thinking']: chunk_callback(text, is_done=False, is_thinking=True),
                                        0
                                    )
                                # Handle text deltas
                                elif 'text' in data['delta']:
                                    sublime.set_timeout(
                                        lambda text=data['delta']['text']: chunk_callback(text, is_done=False, is_thinking=False),
                                        0
                                    )

                            # Get final output tokens from message_delta
                            if data.get('type') == 'message_delta' and 'usage' in data:
                                output_tokens = data['usage'].get('output_tokens', 0)

                            # Send token information at the end
                            if data.get('type') == 'message_stop':
                                # Get cache token information
                                cache_read_tokens = data.get('usage', {}).get('cache_read_input_tokens', 0)
                                cache_write_tokens = data.get('usage', {}).get('cache_write_input_tokens', 0)

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

                                # Build status message with thinking indicator if enabled
                                thinking_info = " (incl. thinking)" if self.thinking_enabled else ""
                                status_message = f"Tokens: {input_tokens:,} sent, {output_tokens:,} received{thinking_info}{cache_info}."

                                if session_stats['cost'] > 0:
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
                            continue # Skip invalid chunks without error messages

            except urllib.error.HTTPError as e:
                error_content = e.read().decode('utf-8')
                print("Claude API Error Content:", error_content)

                # Check if it's a 404 error (model not found)
                if e.code == 404:
                    try:
                        error_data = json.loads(error_content)
                        error_type = error_data.get('error', {}).get('type', '')
                        error_message = error_data.get('error', {}).get('message', '')

                        # Check if the error is about a model not being found
                        if error_type == 'not_found_error' and error_message.startswith('model:'):
                            # Extract the model name from the error message
                            # Format: "model: claude-sonnet-4-5-latest"
                            error_model = error_message.replace('model:', '').strip()

                            # Compare with the currently selected model
                            if error_model == self.model:
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
                                        f'The "{error_model}" model cannot be found.',
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
                                    handle_error(f'[Error] The "{error_model}" model cannot be found. Please update your model via Settings > Package Settings > Claudette > Select Model.')
                            else:
                                # Model in error doesn't match current model, show generic error
                                handle_error("[Error] {0}".format(str(e)))
                        else:
                            handle_error("[Error] {0}".format(str(e)))
                    except (json.JSONDecodeError, AttributeError, KeyError):
                        # If we can't parse the error, show generic 404 error
                        handle_error("[Error] {0}".format(str(e)))
                elif e.code == 401:
                    handle_error("[Error] {0}".format(str(e)))
                else:
                    handle_error("[Error] {0}".format(str(e)))
            except urllib.error.URLError as e:
                handle_error(f"[Error] {str(e)}")
            finally:
                self.spinner.stop()

        except Exception as e:
            sublime.error_message(str(e))
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
