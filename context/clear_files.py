import sublime
import sublime_plugin
from ..utils import claudette_chat_status_message

class ClaudetteContextClearFilesCommand(sublime_plugin.WindowCommand):
    def run(self):
        chat_view = self.get_chat_view()
        if not chat_view:
            sublime.error_message("No active Claudette chat view found")
            return

        included_files = chat_view.settings().get('claudette_context_files', {})
        file_count = len(included_files)

        if sublime.ok_cancel_dialog(
            f"Remove {file_count} file{'s' if file_count != 1 else ''} from the chat context?",
            "Remove Files"
        ):
            chat_view.settings().set('claudette_context_files', {})
            claudette_chat_status_message(self.window, "Included files cleared", "✅")
            sublime.status_message("Included files cleared")

    def get_chat_view(self):
        for view in self.window.views():
            if (view.settings().get('claudette_is_chat_view', False) and
                view.settings().get('claudette_is_current_chat', False)):
                return view
        return None

    def is_visible(self):
        """Controls whether the command appears at all"""
        return bool(self.get_chat_view())

    def is_enabled(self):
        """Controls whether the command is greyed out"""
        chat_view = self.get_chat_view()
        if not chat_view:
            return False
        included_files = chat_view.settings().get('claudette_context_files', {})
        return bool(included_files)
