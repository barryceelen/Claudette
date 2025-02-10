import sublime
import sublime_plugin
import threading
from ..constants import PLUGIN_NAME, SETTINGS_FILE
from ..api.api import ClaudetteClaudeAPI
from ..api.handler import ClaudetteStreamingResponseHandler
from .chat_view import ClaudetteChatView

class ClaudetteAskQuestionCommand(sublime_plugin.TextCommand):
    def __init__(self, view):
        super().__init__(view)
        self.chat_view = None
        self.settings = None
        self._view = view

    def load_settings(self):
        if not self.settings:
            self.settings = sublime.load_settings(SETTINGS_FILE)

    def get_window(self):
        return self._view.window() or sublime.active_window()

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def create_chat_panel(self, force_new=False):
        """
        Creates a chat panel, optionally forcing a new view creation.

        Args:
            force_new (bool): If True, always creates a new view instead of reusing existing one

        Returns:
            sublime.View: The created or existing view
        """
        window = self.get_window()
        if not window:
            print(f"{PLUGIN_NAME} Error: No active window found")
            sublime.error_message(f"{PLUGIN_NAME} Error: No active window found")
            return None

        try:
            if force_new:
                new_view = window.new_file()
                if not new_view:
                    raise Exception("Could not create new view")

                new_view.set_scratch(True)
                new_view.set_name("Claude Chat")
                new_view.assign_syntax('Packages/Markdown/Markdown.sublime-syntax')
                new_view.settings().set('claudette_is_chat_view', True)
                new_view.settings().set('claudette_is_current_chat', True)

                for view in window.views():
                    if view != new_view and view.settings().get('claudette_is_chat_view', False):
                        view.settings().set('claudette_is_current_chat', False)

                # Create a new chat view instance for this view
                self.chat_view = ClaudetteChatView(window, self.settings)
                self.chat_view.view = new_view

                # Register the new instance
                ClaudetteChatView._instances[window.id()] = self.chat_view

                return new_view
            else:
                self.chat_view = ClaudetteChatView.get_instance(window, self.settings)
                return self.chat_view.create_or_get_view()

        except Exception as e:
            print(f"{PLUGIN_NAME} Error: {str(e)}")
            sublime.error_message(f"{PLUGIN_NAME} Error: Could not create or get chat panel")
            return None

    def handle_input(self, code, question):
        if not question or question.strip() == '':
            return None

        if not self.create_chat_panel():
            return

        if not self.settings.get('api_key'):
            self.chat_view.append_text(
                "A Claude API key is required. Please add your API key via Package Settings > Claudette.\n"
            )
            return

        self.send_to_claude(code, question.strip())

    def run(self, edit, code=None, question=None):
        try:
            self.load_settings()

            window = self.get_window()
            if not window:
                print(f"{PLUGIN_NAME} Error: No active window found")
                sublime.error_message(f"{PLUGIN_NAME} Error: No active window found")
                return

            if code is not None and question is not None:
                if not self.create_chat_panel():
                    return
                self.send_to_claude(code, question)
                return

            sel = self.view.sel()
            selected_text = self.view.substr(sel[0]) if sel else ''

            view = window.show_input_panel(
                "Ask Claude:",
                "",
                lambda q: self.handle_input(selected_text, q),
                None,
                None
            )

            if not view:
                print(f"{PLUGIN_NAME} Error: Could not create input panel")
                sublime.error_message(f"{PLUGIN_NAME} Error: Could not create input panel")
                return

        except Exception as e:
            print(f"{PLUGIN_NAME} Error in run command: {str(e)}")
            sublime.error_message(f"{PLUGIN_NAME} Error: Could not process request")

    def send_to_claude(self, code, question):
        try:
            if not self.chat_view:
                return

            message = "\n\n" if self.chat_view.get_size() > 0 else ""
            message += f"## Question\n\n{question}\n\n"

            if code.strip():
                message += f"### Selected Code\n\n```\n{code}\n```\n\n"

            message += "### Claude's Response\n\n"

            user_message = question
            if code.strip():
                user_message = f"{question}\n\nCode:\n{code}"

            conversation = self.chat_view.handle_question(user_message)

            self.chat_view.append_text(message)

            if self.chat_view.get_size() > 0:
                self.chat_view.focus()

            api = ClaudetteClaudeAPI()

            message_start = self.chat_view.view.size()

            def on_complete():
                # Add the response to conversation history after streaming is complete
                response_start = self.chat_view.view.size()
                response_region = sublime.Region(response_start - message_start)
                response_text = self.chat_view.view.substr(response_region)
                self.chat_view.handle_response(response_text)
                self.chat_view.on_streaming_complete()

            handler = ClaudetteStreamingResponseHandler(
                view=self.chat_view.view,
                chat_view=self.chat_view,
                on_complete=on_complete
            )

            thread = threading.Thread(
                target=api.stream_response,
                args=(handler.append_chunk, conversation, self.chat_view.view)
            )

            thread.start()

        except Exception as e:
            print(f"{PLUGIN_NAME} Error sending to Claude: {str(e)}")
            sublime.error_message(f"{PLUGIN_NAME} Error: Could not send message")

class ClaudetteAskNewQuestionCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        try:
            window = self.view.window() or sublime.active_window()
            if not window:
                print(f"{PLUGIN_NAME} Error: No active window found")
                sublime.error_message(f"{PLUGIN_NAME} Error: No active window found")
                return

            ask_command = ClaudetteAskQuestionCommand(self.view)
            ask_command.load_settings()

            if not ask_command.create_chat_panel(force_new=True):
                return

            view = window.show_input_panel(
                "Ask Claude (New Chat):",
                "",
                lambda q: ask_command.handle_input(
                    self.view.substr(self.view.sel()[0]) if self.view.sel() else '',
                    q
                ),
                None,
                None
            )

            if not view:
                print(f"{PLUGIN_NAME} Error: Could not create input panel")
                sublime.error_message(f"{PLUGIN_NAME} Error: Could not create input panel")
                return

        except Exception as e:
            print(f"{PLUGIN_NAME} Error in run command: {str(e)}")
            sublime.error_message(f"{PLUGIN_NAME} Error: Could not process request")
