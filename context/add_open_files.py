import sublime
import sublime_plugin

class ClaudetteContextAddOpenFilesCommand(sublime_plugin.WindowCommand):
    def run(self):
        # Get all open files in the window
        open_files = []
        for view in self.window.views():
            file_name = view.file_name()
            if file_name:
                open_files.append(file_name)

        if not open_files:
            sublime.status_message("No open files found to add to context")
            return

        self.window.run_command('claudette_context_add_files', {'paths': open_files})
