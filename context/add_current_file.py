import sublime
import sublime_plugin
from ..utils import claudette_chat_status_message

class ClaudetteContextAddCurrentFileCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view:
            return

        file_path = view.file_name()
        if not file_path:
            sublime.status_message("Cannot add unsaved file to context")
            return

        self.window.run_command('claudette_context_add_files', {'paths': [file_path]})

    def is_visible(self):
        """Controls whether the command appears at all"""
        view = self.window.active_view()
        if not view:
            return False

        if view.settings().get('claudette_is_chat_view', False):
            return False

        if not view.file_name():
            return False

        return True

    def is_enabled(self):
        """Controls whether the command is greyed out"""
        return self.is_visible()

        import sublime
        import sublime_plugin
        from ..utils import claudette_chat_status_message

class ClaudetteContextRemoveCurrentFileCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view:
            return

        file_path = view.file_name()
        if not file_path:
            return

        # Get the current chat view
        chat_view = self.get_chat_view()
        if not chat_view:
            return

        # Get current context files
        context_files = chat_view.settings().get('claudette_context_files', {})
        if not context_files:
            return

        # Find and remove the file if it exists in context
        removed = False
        updated_files = {}
        for relative_path, file_info in context_files.items():
            if file_info['absolute_path'] != file_path:
                updated_files[relative_path] = file_info
            else:
                removed = True

        if removed:
            chat_view.settings().set('claudette_context_files', updated_files)
            claudette_chat_status_message(self.window, f"Removed {relative_path} from chat context", "✅")
            sublime.status_message(f"Removed file from chat context")
        else:
            sublime.status_message("File not found in chat context")

    def get_chat_view(self):
        for view in self.window.views():
            if (view.settings().get('claudette_is_chat_view', False) and
                view.settings().get('claudette_is_current_chat', False)):
                return view
        return None

    def is_visible(self):
        """Controls whether the command appears at all"""
        view = self.window.active_view()
        if not view:
            return False

        if view.settings().get('claudette_is_chat_view', False):
            return False

        if not view.file_name():
            return False

        # Only show if there's an active chat view
        chat_view = self.get_chat_view()
        if not chat_view:
            return False

        return True

    def is_enabled(self):
        """Controls whether the command is greyed out"""
        if not self.is_visible():
            return False

        # Only enable if the current file is in the context
        view = self.window.active_view()
        file_path = view.file_name()

        chat_view = self.get_chat_view()
        context_files = chat_view.settings().get('claudette_context_files', {})

        # Check if the file exists in context
        for file_info in context_files.values():
            if file_info['absolute_path'] == file_path:
                return True

        return False
