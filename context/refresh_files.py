import sublime
import sublime_plugin
import os
from .file_handler import ClaudetteFileHandler

class ClaudetteContextRefreshFilesCommand(sublime_plugin.WindowCommand):
    def run(self):
        updated_views = 0
        updated_files = 0
        removed_files = 0

        for window in sublime.windows():
            for chat_view in window.views():
                if not chat_view.settings().get('claudette_is_chat_view', False):
                    continue

                context_files = chat_view.settings().get('claudette_context_files', {})
                if not context_files:
                    continue

                file_handler = ClaudetteFileHandler()
                file_handler.files = {}

                view_updated_count = 0
                view_removed_count = 0

                for relative_path, file_info in context_files.items():
                    file_path = file_info['absolute_path']

                    # Skip files that no longer exist
                    if not os.path.exists(file_path):
                        view_removed_count += 1
                        continue

                    root_folder = file_path[:file_path.rindex(relative_path)]
                    if file_handler.process_file(file_path, root_folder):
                        view_updated_count += 1

                if view_updated_count > 0 or view_removed_count > 0:
                    chat_view.settings().set('claudette_context_files', file_handler.files)
                    if view_updated_count > 0:
                        updated_views += 1
                        updated_files += view_updated_count
                    if view_removed_count > 0:
                        removed_files += view_removed_count

        message_parts = []

        if updated_files > 0:
            message_parts.append(
                f"Updated {updated_files} {'file' if updated_files == 1 else 'files'} "
                f"in {updated_views} {'view' if updated_views == 1 else 'views'}"
            )

        if removed_files > 0:
            message_parts.append(
                f"Removed {removed_files} missing {'file' if removed_files == 1 else 'files'}"
            )

        if message_parts:
            sublime.status_message(", ".join(message_parts))
        else:
            sublime.status_message("No files needed updating")

    def is_enabled(self):
        """Enable command if any chat view has context files"""
        for window in sublime.windows():
            for view in window.views():
                if (view.settings().get('claudette_is_chat_view', False) and
                    bool(view.settings().get('claudette_context_files', {}))):
                    return True
        return False
