import sublime
import json
import urllib.request
import urllib.parse
import urllib.error
from ..constants import ANTHROPIC_VERSION, DEFAULT_MODEL, MAX_TOKENS, SETTINGS_FILE
from ..statusbar.spinner import Spinner

class ClaudeAPI:
    BASE_URL = 'https://api.anthropic.com/v1/'

    def __init__(self):
        self.settings = sublime.load_settings(SETTINGS_FILE)
        self.api_key = self.settings.get('api_key')
        self.max_tokens = self.settings.get('max_tokens', MAX_TOKENS)
        self.model = self.settings.get('model', DEFAULT_MODEL)
        self.spinner = Spinner()
        self.temperature = self.settings.get('temperature', '1.0')

    @staticmethod
    def get_valid_temperature(temp):
        try:
            temp = float(temp)
            if 0.0 <= temp <= 1.0:
                return temp
            return 1.0
        except (TypeError, ValueError):
            return 1.0

    def stream_response(self, chunk_callback, messages):
        """Stream API response for the given messages."""
        if not messages or not any(msg.get('content', '').strip() for msg in messages):
            return

        def handle_error(error_msg):
            sublime.set_timeout(
                lambda msg=error_msg: chunk_callback(msg),
                0
            )

        try:
            self.spinner.start('Fetching response')
            headers = {
                'x-api-key': self.api_key,
                'anthropic-version': ANTHROPIC_VERSION,
                'content-type': 'application/json',
            }

            # Filter out empty messages
            filtered_messages = [
                msg for msg in messages
                if msg.get('content', '').strip()
            ]

            data = {
            	'messages': filtered_messages,
            	'max_tokens': MAX_TOKENS,
                'model': self.model,
                'stream': True,
                'system': 'Please wrap all code examples in a markdown code block and ensure each code block is complete and self-contained.',
                'temperature': self.get_valid_temperature(self.temperature)
            }

            system_messages = self.settings.get('system_messages', [])
            default_index = self.settings.get('default_system_message_index', 0)

            if (system_messages and
                isinstance(system_messages, list) and
                isinstance(default_index, int) and
                0 <= default_index < len(system_messages)):

                selected_message = system_messages[default_index]
                if selected_message and selected_message.strip():
                    data['system'] = data['system'] + ' ' + selected_message.strip()

            req = urllib.request.Request(
                urllib.parse.urljoin(self.BASE_URL, 'messages'),
                data=json.dumps(data).encode('utf-8'),
                headers=headers,
                method='POST'
            )

            try:
                with urllib.request.urlopen(req) as response:
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
                            if 'delta' in data and 'text' in data['delta']:
                                sublime.set_timeout(
                                    lambda text=data['delta']['text']: chunk_callback(text),
                                    0
                                )
                        except Exception:
                            continue # Skip invalid chunks without error messages

            except urllib.error.HTTPError as e:
                error_content = e.read().decode('utf-8')
                print("Claude API Error Content:", error_content)
                if e.code == 401:
                    handle_error("[Error] {0}".format(str(e)))
                else:
                    handle_error("[Error] {0}".format(str(e)))
            except urllib.error.URLError as e:
                handle_error("[Error] {0}".format(str(e)))
            finally:
                self.spinner.stop()

        except Exception as e:
            sublime.error_message(str(e))
            self.spinner.stop()

    def fetch_models(self):
        try:
            sublime.status_message('Fetching models')
            headers = {
                'x-api-key': self.api_key,
                'anthropic-version': ANTHROPIC_VERSION,
            }

            req = urllib.request.Request(
                urllib.parse.urljoin(self.BASE_URL, 'models'),
                headers=headers,
                method='GET'
            )

            with urllib.request.urlopen(req) as response:
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
