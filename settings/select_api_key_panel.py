import sublime
import sublime_plugin
from ..constants import SETTINGS_FILE

class ClaudetteSelectApiKeyPanelCommand(sublime_plugin.WindowCommand):
    """
    A command to switch between different API keys.

    This command shows a quick panel with available API keys and allows the user
    to select and switch to a different API key.
    """
    def is_visible(self):
        return True

    def is_enabled(self):
        settings = sublime.load_settings(SETTINGS_FILE)
        api_key = settings.get('api_key')

        # Check if the api_key is a dictionary containing keys, with at least two keys
        return (isinstance(api_key, dict) and
                api_key.get('keys') and
                isinstance(api_key['keys'], list) and
                len(api_key['keys']) > 1)

    def run(self):
        try:
            settings = sublime.load_settings(SETTINGS_FILE)
            api_key = settings.get('api_key')
            panel_items = []

            if isinstance(api_key, str) and api_key.strip():
                # We have a single key
                panel_items.append(["Default"])
            elif isinstance(api_key, dict) and api_key.get('keys') and isinstance(api_key['keys'], list):
                for i, key_entry in enumerate(api_key['keys']):
                    if isinstance(key_entry, dict) and key_entry.get('key'):
                        name = key_entry.get('name', f"Untitled {i}" if i > 0 else "Untitled")
                        panel_items.append([name])

            # Add the appropriate settings item based on whether api_key exists
            settings_item = "→ Manage API keys" if panel_items else "＋ Add new API key"
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
                    if isinstance(api_key, str):
                        # Nothing needed for a single API key.
                        pass
                    elif isinstance(api_key, dict) and api_key.get('keys'):
                        updated_api_key = api_key.copy()
                        updated_api_key['active_key'] = index
                        settings.set('api_key', updated_api_key)
                        sublime.save_settings(SETTINGS_FILE)

                    sublime.status_message(f"Switched to API key: {panel_items[index][0]}")

            selection_index = 0
            if isinstance(api_key, dict) and api_key.get('keys'):
                current_index = api_key.get('default_index', 0)
                if 0 <= current_index < len(panel_items) - 1:  # -1 for settings item
                    selection_index = current_index

            self.window.show_quick_panel(
                panel_items,
                on_select,
                0,
                selection_index
            )

        except Exception as e:
            sublime.error_message(f"Error showing API key selection panel: {str(e)}")
