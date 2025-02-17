import sublime
import sublime_plugin
import os
from .file_handler import ClaudetteFileHandler

from ..utils import claudette_chat_status_message

class ClaudetteContextAddFilesCommand(sublime_plugin.WindowCommand):
    def run(self, paths=None):
        if not paths:
            return

        chat_view = self.get_chat_view()
        if not chat_view:
            return

        file_handler = ClaudetteFileHandler()
        file_handler.files = chat_view.settings().get('claudette_context_files', {})

        # Ensure paths is always a list
        if isinstance(paths, str):
            paths = [paths]

        # Expand directories into file paths
        expanded_paths = []
        for path in paths:
            if os.path.isdir(path):
                for root, _, files in os.walk(path):
                    for file in files:
                        expanded_paths.append(os.path.join(root, file))
            else:
                expanded_paths.append(path)

        result = file_handler.process_paths(expanded_paths)

        chat_view.settings().set('claudette_context_files', result['files'])

        # Update status message to include directories
        dirs_count = sum(1 for p in paths if os.path.isdir(p))
        files_count = sum(1 for p in paths if os.path.isfile(p))

        message_parts = []
        if dirs_count > 0:
            message_parts.append(f"{dirs_count} {'directory' if dirs_count == 1 else 'directories'}")
        if files_count > 0:
            message_parts.append(f"{files_count} {'file' if files_count == 1 else 'files'}")

        message = f"Included {' and '.join(message_parts)}"

        if result['processed_files'] > 0:
            message += f" ({result['processed_files']} total files processed)"
        if result['skipped_files'] > 0:
            message += f", skipped {result['skipped_files']} files"

        claudette_chat_status_message(self.window, message, "✅")
        sublime.status_message(message)

    def get_chat_view(self):
        for view in self.window.views():
            if (view.settings().get('claudette_is_chat_view', False) and
                view.settings().get('claudette_is_current_chat', False)):
                return view
        return None

    def is_visible(self, paths=None):
        """Controls whether the command appears in the context menu"""
        return True

    def is_enabled(self, paths=None):
        """Controls whether the command is greyed out"""
        return bool(self.get_chat_view() and paths)
