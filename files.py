import os
import mimetypes
from pathlib import Path
import sublime
import sublime_plugin
import json

class FileContentManager:
    def __init__(self):
        self.files = {}  # Dictionary to store file paths and their contents
        self.total_tokens = 0
        self.max_tokens = 100000

    def is_text_file(self, file_path):
        """Check if a file is a text file using mimetype."""
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type is None:
            text_extensions = {'.txt', '.py', '.js', '.html', '.css', '.json', '.md',
                             '.csv', '.xml', '.yml', '.yaml', '.ini', '.conf', '.sh',
                             '.rb', '.php', '.java', '.cpp', '.h', '.c', '.rs', '.go'}
            return Path(file_path).suffix.lower() in text_extensions
        return mime_type.startswith('text/')

    def estimate_tokens(self, text):
        """Estimate tokens based on character count (rough approximation)."""
        return len(text) // 4

    def process_file(self, file_path, root_folder):
        """Process a single file and add its content to the files dictionary."""
        try:
            if not self.is_text_file(file_path):
                return None

            relative_path = os.path.relpath(file_path, root_folder)

            # Skip if file is already included
            if relative_path in self.files:
                return None

            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            file_content = f"\nFile: {relative_path}\n```\n{content}\n```\n"
            tokens = self.estimate_tokens(file_content)

            if self.total_tokens + tokens > self.max_tokens:
                return None

            self.files[relative_path] = {
                'content': file_content,
                'tokens': tokens,
                'absolute_path': file_path
            }
            self.total_tokens += tokens
            return relative_path

        except Exception as e:
            print(f"Error processing file {file_path}: {str(e)}")
            return None

    def process_paths(self, paths):
        """Process multiple files or folders."""
        # Get the common root folder
        if len(paths) == 1:
            root_folder = os.path.dirname(paths[0]) if os.path.isfile(paths[0]) else paths[0]
        else:
            root_folder = os.path.commonpath(paths)

        processed_files = 0
        skipped_files = 0

        for path in paths:
            if os.path.isfile(path):
                if self.process_file(path, root_folder):
                    processed_files += 1
                else:
                    skipped_files += 1
            elif os.path.isdir(path):
                for root, _, files in os.walk(path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        if self.process_file(file_path, root_folder):
                            processed_files += 1
                        else:
                            skipped_files += 1

        return {
            'files': self.files,
            'total_tokens': self.total_tokens,
            'processed_files': processed_files,
            'skipped_files': skipped_files
        }

class ClaudetteIncludeInContextCommand(sublime_plugin.WindowCommand):
    def run(self, paths=None, files=None):
        if not paths and not files:
            return

        target_paths = paths or files
        file_manager = FileContentManager()

        # Load existing files if any
        chat_view = self.get_chat_view()
        if not chat_view:
            sublime.error_message("Please open a Claudette chat view first")
            return

        existing_files = chat_view.settings().get('claudette_included_files', {})
        file_manager.files = existing_files
        file_manager.total_tokens = sum(f['tokens'] for f in existing_files.values())

        result = file_manager.process_paths(target_paths)

        # Store the updated files dictionary in the view's settings
        chat_view.settings().set('claudette_included_files', result['files'])

        # Show summary in status bar
        msg = f"Included {result['processed_files']} files (~{result['total_tokens']} tokens)"
        if result['skipped_files'] > 0:
            msg += f", skipped {result['skipped_files']} files"
        sublime.status_message(msg)

    def get_chat_view(self):
        for view in self.window.views():
            if (view.settings().get('claudette_is_chat_view', False) and
                view.settings().get('claudette_is_current_chat', False)):
                return view
        return None

    def is_visible(self, paths=None, files=None):
        return bool(paths or files)

class ClaudetteClearIncludedFilesCommand(sublime_plugin.WindowCommand):
    def run(self):
        chat_view = self.get_chat_view()
        if not chat_view:
            sublime.error_message("No active Claudette chat view found")
            return

        included_files = chat_view.settings().get('claudette_included_files', {})
        file_count = len(included_files)

        # Show confirmation dialog
        if sublime.ok_cancel_dialog(
            f"Clear {file_count} file{'s' if file_count != 1 else ''} from context?",
            "Clear Files"
        ):
            chat_view.settings().set('claudette_included_files', {})
            sublime.status_message("Cleared all included files from context")

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
        included_files = chat_view.settings().get('claudette_included_files', {})
        return bool(included_files)

class ClaudetteShowIncludedFilesCommand(sublime_plugin.WindowCommand):
    def run(self):
        chat_view = self.get_chat_view()
        if not chat_view:
            sublime.error_message("No active Claudette chat view found")
            return

        self.included_files = chat_view.settings().get('claudette_included_files', {})
        if not self.included_files:
            sublime.status_message("No files are currently included in the context")
            return

        # Create items list for quick panel
        self.items = list(self.included_files.keys())

        # Show quick panel with files
        self.window.show_quick_panel(
            items=self.items,
            on_select=self.on_file_selected,
            flags=sublime.KEEP_OPEN_ON_FOCUS_LOST
        )

    def on_file_selected(self, index):
        if index == -1:
            return

        selected_file = self.items[index]
        file_info = self.included_files[selected_file]

        # Show options for the selected file
        self.selected_file = selected_file
        options = [
            f"📂 Open {selected_file}",
            f"ℹ️ Details ({file_info['tokens']} tokens)",
            f"❌ Remove from context"
        ]

        self.window.show_quick_panel(
            items=options,
            on_select=self.on_option_selected,
            flags=sublime.KEEP_OPEN_ON_FOCUS_LOST
        )

    def on_option_selected(self, index):
        if index == -1:
            return

        file_info = self.included_files[self.selected_file]

        if index == 0:  # Open file
            self.window.open_file(file_info['absolute_path'])
        elif index == 1:  # Show details
            details = (
                f"File: {self.selected_file}\n"
                f"Absolute path: {file_info['absolute_path']}\n"
                f"Tokens: {file_info['tokens']}"
            )
            sublime.message_dialog(details)
        elif index == 2:  # Remove from context
            chat_view = self.get_chat_view()
            if chat_view:
                # Remove the file from the dictionary
                self.included_files.pop(self.selected_file)
                # Update the view settings
                chat_view.settings().set('claudette_included_files', self.included_files)
                # Show confirmation
                sublime.status_message(f"Removed {self.selected_file} from context")

                # If there are still files, show the file list again
                if self.included_files:
                    sublime.set_timeout(lambda: self.run(), 100)

    def get_chat_view(self):
        for view in self.window.views():
            if (view.settings().get('claudette_is_chat_view', False) and
                view.settings().get('claudette_is_current_chat', False)):
                return view
        return None

    def is_visible(self):
        chat_view = self.get_chat_view()
        return bool(chat_view)

    def is_enabled(self):
        """Controls whether the command is greyed out"""
        chat_view = self.get_chat_view()
        if not chat_view:
            return False
        included_files = chat_view.settings().get('claudette_included_files', {})
        return bool(included_files)
