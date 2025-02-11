import sublime
import sublime_plugin
from .file_handler import FileHandler

class ClaudetteContextFileWatcher(sublime_plugin.EventListener):
    last_update = 0

    def on_post_save_async(self, view):
        """Called asynchronously after a view is saved"""
        settings = sublime.load_settings('Claudette.sublime-settings')
        if not settings.get('context_auto_update_files', True):
            return

        current_time = time()

        # 1 second debounce
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

                context_files = chat_view.settings().get('claudette_context_files', {})

                for relative_path, file_info in context_files.items():
                    if file_info['absolute_path'] == file_path:
                        file_handler = FileHandler()
                        file_handler.files = context_files

                        root_folder = file_path[:file_path.rindex(relative_path)]
                        file_handler.process_file(file_path, root_folder)

                        chat_view.settings().set('claudette_context_files', file_handler.files)

                        sublime.status_message(f"Updated {relative_path} in chat context")

class ClaudetteContextRefreshFilesCommand(sublime_plugin.WindowCommand):
    def run(self):
        chat_view = self.get_chat_view()
        if not chat_view:
            return

        context_files = chat_view.settings().get('claudette_context_files', {})
        if not context_files:
            return

        file_handler = FileHandler()
        file_handler.files = context_files.copy()

        updated_count = 0
        for relative_path, file_info in context_files.items():
            file_path = file_info['absolute_path']
            root_folder = file_path[:file_path.rindex(relative_path)]
            if file_handler.process_file(file_path, root_folder):
                updated_count += 1

        chat_view.settings().set('claudette_context_files', file_handler.files)

        sublime.status_message(f"Updated {updated_count} files in chat context")

    def get_chat_view(self):
        for view in self.window.views():
            if (view.settings().get('claudette_is_chat_view', False) and
                view.settings().get('claudette_is_current_chat', False)):
                return view
        return None
