import sublime
import sublime_plugin
from ..constants import SETTINGS_FILE, AUTH_MODE_API_KEY, AUTH_MODE_OAUTH, DEFAULT_AUTH_MODE
from ..utils import claudette_validate_oauth_setup, claudette_get_api_key_value

class ClaudetteSelectAuthModePanelCommand(sublime_plugin.WindowCommand):
    """
    A command to switch between API key and OAuth authentication modes.

    This command shows a quick panel allowing the user to select their
    preferred authentication method.
    """

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def run(self):
        try:
            settings = sublime.load_settings(SETTINGS_FILE)
            current_mode = settings.get('auth_mode', DEFAULT_AUTH_MODE)

            # Build panel items with status indicators
            panel_items = []

            # API Key option
            api_key_status = ""
            if claudette_get_api_key_value():
                api_key_status = " (configured)"
            else:
                api_key_status = " (not configured)"
            panel_items.append([
                "API Key" + api_key_status,
                "Use x-api-key header with your Anthropic API key"
            ])

            # OAuth option
            oauth_valid, oauth_error = claudette_validate_oauth_setup()
            oauth_status = " (ready)" if oauth_valid else " (not configured)"
            panel_items.append([
                "OAuth / Web Login" + oauth_status,
                "Use CLAUDE_CODE_OAUTH_TOKEN env var or ~/.claude/.credentials.json"
            ])

            # Add settings/help item
            panel_items.append([
                "Help: How to set up OAuth",
                "Shows instructions for configuring OAuth authentication"
            ])

            # Determine current selection index
            selection_index = 0 if current_mode == AUTH_MODE_API_KEY else 1

            def on_select(index):
                if index == -1:
                    return

                if index == 0:
                    # API Key mode selected
                    if not claudette_get_api_key_value():
                        sublime.message_dialog(
                            "No API key configured.\n\n"
                            "Please add your API key in Settings > Package Settings > Claudette > Settings"
                        )
                        return
                    settings.set('auth_mode', AUTH_MODE_API_KEY)
                    sublime.save_settings(SETTINGS_FILE)
                    sublime.status_message("Switched to API Key authentication")

                elif index == 1:
                    # OAuth mode selected
                    is_valid, error_msg = claudette_validate_oauth_setup()
                    if not is_valid:
                        sublime.message_dialog(
                            f"OAuth is not properly configured.\n\n{error_msg}\n\n"
                            "To set up OAuth:\n"
                            "1. Install Claude Code CLI\n"
                            "2. Run 'claude setup-token' in your terminal\n"
                            "3. Set the CLAUDE_CODE_OAUTH_TOKEN environment variable\n"
                            "4. Restart Sublime Text and select OAuth mode"
                        )
                        return
                    settings.set('auth_mode', AUTH_MODE_OAUTH)
                    sublime.save_settings(SETTINGS_FILE)
                    sublime.status_message("Switched to OAuth authentication")

                elif index == 2:
                    # Help selected - show documentation
                    sublime.message_dialog(
                        "Setting up OAuth Authentication\n"
                        "=" * 35 + "\n\n"
                        "OAuth allows Pro/Max subscribers to use Claudette without an API key.\n\n"
                        "Recommended setup (works on all platforms):\n"
                        "1. Install Claude Code CLI (npm install -g @anthropic-ai/claude-code)\n"
                        "2. Run 'claude setup-token' in your terminal\n"
                        "3. Set the CLAUDE_CODE_OAUTH_TOKEN environment variable\n"
                        "   with the token value it provides\n"
                        "4. Restart Sublime Text\n"
                        "5. Select 'OAuth / Web Login' in this panel\n\n"
                        "Alternative (Linux only):\n"
                        "Run 'claude login' - credentials are saved to\n"
                        "~/.claude/.credentials.json and used automatically.\n"
                        "(macOS stores tokens in Keychain instead, so use\n"
                        "the environment variable method above.)"
                    )

            self.window.show_quick_panel(
                panel_items,
                on_select,
                sublime.MONOSPACE_FONT,
                selection_index
            )

        except Exception as e:
            print(f"Claudette: Error showing auth mode selection panel: {str(e)}")
            sublime.error_message(f"Error showing auth mode selection panel: {str(e)}")
