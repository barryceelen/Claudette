import sublime
import sublime_plugin
from ..utils import claudette_chat_status_message

class ClaudetteContextManageFilesCommand(sublime_plugin.WindowCommand):
    def run(self):
        chat_view = self.get_chat_view()
        if not chat_view:
            sublime.error_message("No active Claudette chat view found")
            return

        self.included_files = chat_view.settings().get('claudette_context_files', {})

        if not self.included_files:
            sublime.status_message("No files are included in the chat context")
            return

        self.items = list(self.included_files.keys())

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

        self.selected_file = selected_file

        options = [
            f"Open {selected_file}",
            f"Remove from context"
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

        if index == 0: # Open file
            self.window.open_file(file_info['absolute_path'])
        elif index == 1: # Remove from context
            chat_view = self.get_chat_view()
            if chat_view:
                self.included_files.pop(self.selected_file)
                chat_view.settings().set('claudette_context_files', self.included_files)
                claudette_chat_status_message(self.window, f"Removed {self.selected_file} from the chat context", "✅")
                sublime.status_message(f"Removed {self.selected_file} from the chat context")

                if self.included_files:
                    sublime.set_timeout(lambda: self.run(), 100)

    def get_chat_view(self):
        for view in self.window.views():
            if (view.settings().get('claudette_is_chat_view', False) and
                view.settings().get('claudette_is_current_chat', False)):
                return view
        return None

    def is_visible(self):
        """Controls whether the command appears at all"""
        chat_view = self.get_chat_view()
        return bool(chat_view)

    def is_enabled(self):
        """Controls whether the command is greyed out"""
        chat_view = self.get_chat_view()
        if not chat_view:
            return False
        included_files = chat_view.settings().get('claudette_context_files', {})
        return bool(included_files)
