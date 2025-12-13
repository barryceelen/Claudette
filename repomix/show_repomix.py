import sublime
import sublime_plugin

class ClaudetteShowRepomixCommand(sublime_plugin.WindowCommand):
    def get_active_chat_view(self):
        for view in self.window.views():
            if (view.settings().get('claudette_is_chat_view', False) and
                view.settings().get('claudette_is_current_chat', False)):
                return view
        return None

    def is_visible(self):
        return self.get_active_chat_view() is not None

    def run(self):
        chat_view = self.get_active_chat_view()
        if not chat_view:
            sublime.message_dialog("No active chat view found")
            return

        repomix_content = chat_view.settings().get('claudette_repomix')
        if not repomix_content:
            sublime.message_dialog("No Repomix content available for the active chat view")
            return

        # Create a new scratch view for the repomix content
        repomix_view = self.window.new_file()
        repomix_view.set_name("Repomix Output")
        repomix_view.set_scratch(True)

        # Insert the content
        repomix_view.run_command('append', {
            'characters': repomix_content,
            'force': True
        })

        # Set syntax to markdown since repomix output uses markdown formatting
        repomix_view.assign_syntax('Packages/Markdown/Markdown.sublime-syntax')
