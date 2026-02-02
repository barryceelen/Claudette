import sublime
import sublime_plugin
from ..constants import SETTINGS_FILE


class ClaudetteToggleThinkingCommand(sublime_plugin.WindowCommand):
    """
    A command to toggle Claude's extended thinking mode on or off.
    """

    def run(self):
        try:
            settings = sublime.load_settings(SETTINGS_FILE)
            thinking_settings = settings.get('thinking', {})
            is_enabled = thinking_settings.get('enabled', False)
            budget_tokens = thinking_settings.get('budget_tokens', 10000)

            # Toggle the state
            new_enabled = not is_enabled
            settings.set('thinking', {
                'enabled': new_enabled,
                'budget_tokens': budget_tokens
            })

            sublime.save_settings(SETTINGS_FILE)

            status = 'enabled' if new_enabled else 'disabled'
            sublime.status_message(f'Thinking mode {status}')

        except Exception as e:
            print(f'Error toggling thinking mode: {str(e)}')
            sublime.error_message(f'Error toggling thinking mode: {str(e)}')

    def description(self):
        """Return dynamic description based on current thinking state."""
        settings = sublime.load_settings(SETTINGS_FILE)
        thinking_settings = settings.get('thinking', {})
        is_enabled = thinking_settings.get('enabled', False)

        if is_enabled:
            return 'Claudette: Disable Thinking Mode'
        else:
            return 'Claudette: Enable Thinking Mode'
