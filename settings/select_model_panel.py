import sublime
import sublime_plugin
from ..api.api import ClaudetteClaudeAPI
from ..constants import SETTINGS_FILE

class ClaudetteSelectModelPanelCommand(sublime_plugin.WindowCommand):
    """
    A command to switch between different Claude AI models.

    This command shows a quick panel with available Claude models
    and allows the user to select and switch to a different model.
    """

    def is_visible(self):
        return True

    def run(self):
        try:
            api = ClaudetteClaudeAPI()
            settings = sublime.load_settings(SETTINGS_FILE)
            current_model = settings.get('model')
            models = api.fetch_models()

            if current_model in models:
                selected_index = models.index(current_model)
            else:
                models.insert(0, current_model)
                selected_index = 0

            def on_select(index):
                if index != -1:
                    try:
                        selected_model = models[index]
                        settings.set('model', selected_model)
                        # Save settings to persist the change
                        sublime.save_settings(SETTINGS_FILE)
                        # Verify the setting was saved
                        saved_model = settings.get('model')
                        if saved_model == selected_model:
                            sublime.status_message("Claude model switched to {0}".format(str(selected_model)))
                        else:
                            print(f"Warning: Model setting may not have saved correctly. Expected: {selected_model}, Got: {saved_model}")
                            sublime.status_message("Claude model switched to {0} (please verify settings were saved)".format(str(selected_model)))
                    except Exception as e:
                        print(f"Error saving model setting: {str(e)}")
                        sublime.error_message(f"Error saving model setting: {str(e)}")

            self.window.show_quick_panel(models, on_select, 0, selected_index)
        except Exception as e:
            print(f"Error showing model selection panel: {str(e)}")
            sublime.error_message(f"Error showing model selection panel: {str(e)}")
