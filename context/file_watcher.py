import sublime
import sublime_plugin
import os
from .file_handler import FileHandler
from time import time

class ClaudetteContextFileWatcher(sublime_plugin.EventListener):
    last_update = 0

    def on_post_save_async(self, view):
        """Called asynchronously after any view is saved"""
        print(f"ClaudetteContextFileWatcher: File saved: {view.file_name()}")
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

        # Check all chat views for this file
        for window in sublime.windows():
            for chat_view in window.views():
                if not chat_view.settings().get('claudette_is_chat_view', False):
                    continue

                self._update_file_if_in_context(chat_view, file_path)

    def on_window_command(self, window, command_name, args):
        """Handle file moves, renames, and deletions"""
        print(f"ClaudetteContextFileWatcher: Window command: {command_name}, args: {args}")
        if command_name in ('rename_path', 'side_bar_rename', 'rename_file'):
            self._handle_file_move(window, args)
        elif command_name in ('delete_file', 'side_bar_delete'):
            self._handle_file_deletion(window, args)

    def _handle_file_move(self, window, args):
        """Handle file move and rename operations"""
        old_path = args.get('old_path') or args.get('from')
        new_path = args.get('new_path') or args.get('to')

        if not (old_path and new_path):
            return

        # Update file in all chat views
        for chat_view in window.views():
            if not chat_view.settings().get('claudette_is_chat_view', False):
                continue

            context_files = chat_view.settings().get('claudette_context_files', {})
            updated = False
            updated_files = {}

            for relative_path, file_info in context_files.items():
                abs_path = file_info['absolute_path']

                if abs_path == old_path:
                    if not os.path.exists(new_path):
                        continue

                    file_info['absolute_path'] = new_path
                    try:
                        new_relative_path = os.path.relpath(
                            new_path,
                            os.path.dirname(new_path)
                        )
                        updated_files[new_relative_path] = file_info
                        updated = True
                    except ValueError:
                        updated_files[relative_path] = file_info
                else:
                    updated_files[relative_path] = file_info

            if updated:
                chat_view.settings().set('claudette_context_files', updated_files)
                sublime.status_message(f"Updated moved file in chat context")

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
                file_handler = FileHandler()
                file_handler.files = context_files

                root_folder = file_path[:file_path.rindex(relative_path)]
                file_handler.process_file(file_path, root_folder)

                chat_view.settings().set('claudette_context_files', file_handler.files)
                sublime.status_message(f"Updated {relative_path} in chat context")
