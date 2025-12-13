import sublime
import sublime_plugin
from ..utils import claudette_chat_status_message

class ClaudetteClearRepomixCommand(sublime_plugin.WindowCommand):
    """Command to clear Repomix content from the active chat view."""

    def get_active_chat_view(self):
        """Get the current active chat view."""
        for view in self.window.views():
            if (view.settings().get('claudette_is_chat_view', False) and
                view.settings().get('claudette_is_current_chat', False)):
                return view
        return None

    def is_visible(self):
        """Always show the command in menus."""
        return True

    def is_enabled(self):
        """Enable command only if there's Repomix content to clear."""
        chat_view = self.get_active_chat_view()
        if not chat_view:
            return False
        return chat_view.settings().get('claudette_repomix') is not None

    def run(self):
        """Clear Repomix content from the active chat view."""
        chat_view = self.get_active_chat_view()
        if not chat_view:
            sublime.status_message("No active chat view found")
            return

        chat_view.settings().erase('claudette_repomix')
        chat_view.settings().erase('claudette_repomix_tokens')

        claudette_chat_status_message(window, "Repomix content cleared", prefix="✅")
        sublime.status_message("Repomix content cleared")
