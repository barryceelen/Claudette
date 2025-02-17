import sublime
import sublime_plugin
import os
from .file_handler import ClaudetteFileHandler
from time import time

class ClaudetteContextFileWatcher(sublime_plugin.EventListener):
    """Very basic file watcher."""
    last_update = 0

    def on_post_save_async(self, view):
        """Called asynchronously after any view is saved"""
        settings = sublime.load_settings('Claudette.sublime-settings')
        if not settings.get('chat.context.auto_update_files', True):
            return

        current_time = time()
        if current_time - self.last_update < 1.0:
            return
        self.last_update = current_time

        file_path = view.file_name()
        if not file_path:
            return

        for window in sublime.windows():
            for chat_view in window.views():
                if not chat_view.settings().get('claudette_is_chat_view', False):
                    continue

                self._update_file_if_in_context(chat_view, file_path)

    def on_post_window_command(self, window, command_name, args):
        """Handle file deletions"""
        if command_name in ('delete_file', 'side_bar_delete'):
            self._handle_file_deletion(window, args)

    def _handle_file_deletion(self, window, args):
        """Handle file deletion operations"""
        paths = args.get('paths', [])
        if isinstance(paths, str):
            paths = [paths]

        if not paths:
            return

        # Update all chat views
        for chat_view in window.views():
            if not chat_view.settings().get('claudette_is_chat_view', False):
                continue

            context_files = chat_view.settings().get('claudette_context_files', {})
            updated_files = {}
            removed_count = 0

            for relative_path, file_info in context_files.items():
                abs_path = file_info['absolute_path']
                if abs_path not in paths and os.path.exists(abs_path):
                    updated_files[relative_path] = file_info
                else:
                    removed_count += 1

            if removed_count > 0:
                chat_view.settings().set('claudette_context_files', updated_files)
                plural = 's' if removed_count > 1 else ''
                sublime.status_message(
                    f"Removed {removed_count} deleted file{plural} from chat context"
                )

    def _update_file_if_in_context(self, chat_view, file_path):
        """Helper method to update a file if it exists in the context"""
        if not os.path.exists(file_path):
            self._handle_file_deletion(chat_view.window(), {'paths': [file_path]})
            return

        context_files = chat_view.settings().get('claudette_context_files', {})

        for relative_path, file_info in context_files.items():
            if file_info['absolute_path'] == file_path:
                file_handler = ClaudetteFileHandler()
                file_handler.files = context_files

                root_folder = file_path[:file_path.rindex(relative_path)]
                file_handler.process_file(file_path, root_folder)

                chat_view.settings().set('claudette_context_files', file_handler.files)
                sublime.status_message(f"Updated {relative_path} in chat context")
