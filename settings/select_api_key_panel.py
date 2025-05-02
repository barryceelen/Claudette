import sublime
import sublime_plugin
from ..constants import SETTINGS_FILE
from ..utils import claudette_get_valid_api_keys

class ClaudetteSelectApiKeyPanelCommand(sublime_plugin.WindowCommand):
    """
    A command to switch between different API keys.

    This command shows a quick panel with available API keys and allows the user
    to select and switch to a different API key.
    """
    def is_visible(self):
        return True

    def run(self):
        try:
            settings = sublime.load_settings(SETTINGS_FILE)
            current_index = settings.get('default_api_key_index', 0)
            api_keys = settings.get('api_keys', [])
            valid_api_keys = claudette_get_valid_api_keys()

            panel_items = [[item['name']] for item in valid_api_keys]

            # Add the appropriate settings item based on whether api_keys exist
            settings_item = "→ Manage API keys" if valid_api_keys else "＋ Add new API key"
            panel_items.append([settings_item])

            def on_select(index):
                if index == -1:
                    return

                if index == len(panel_items) - 1:
                    # Open package settings if the last item was selected
                    self.window.run_command("edit_settings", {
                        "base_file": "${packages}/Claudette/Claudette.sublime-settings",
                        "default": "{\n\t$0\n}\n"
                    })
                else:
                    # Update the default API key index to match the filtered list
                    api_keys = settings.get('api_keys', [])  # Get fresh copy in case it was updated
                    selected_api_key = valid_api_keys[index]['api_key']
                    settings.set('default_api_key_index', api_keys.index(selected_api_key))
                    sublime.save_settings(SETTINGS_FILE)
                    sublime.status_message(f"Switched to API key: {valid_api_keys[index]['name']}")

            # Determine the correct selection index for the current API key
            selection_index = 0
            if current_index < len(api_keys):
                current_api_key = api_keys[current_index]
                for i, item in enumerate(valid_api_keys):
                    if item['api_key'] == current_api_key:
                        selection_index = i
                        break

            self.window.show_quick_panel(
                panel_items,
                on_select,
                0,
                selection_index
            )

        except Exception as e:
            sublime.error_message(f"Error showing API key selection panel: {str(e)}")
