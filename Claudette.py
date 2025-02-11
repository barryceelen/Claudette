#      ___/\/\/\/\/\__/\/\__________________________________/\/\________________/\/\________/\/\_________________
#     _/\/\__________/\/\____/\/\/\______/\/\__/\/\________/\/\____/\/\/\____/\/\/\/\/\__/\/\/\/\/\____/\/\/\___
#    _/\/\__________/\/\________/\/\____/\/\__/\/\____/\/\/\/\__/\/\/\/\/\____/\/\________/\/\______/\/\/\/\/\_
#   _/\/\__________/\/\____/\/\/\/\____/\/\__/\/\__/\/\__/\/\__/\/\__________/\/\________/\/\______/\/\_______
#  ___/\/\/\/\/\__/\/\/\__/\/\/\/\/\____/\/\/\/\____/\/\/\/\____/\/\/\/\____/\/\/\______/\/\/\______/\/\/\/\_
# __________________________________________________________________________________________________________

import sublime
import sublime_plugin

from .chat.chat_view import ClaudetteChatViewListener
from .chat.ask_question import ClaudetteAskQuestionCommand, ClaudetteAskNewQuestionCommand
from .chat.chat_history import ClaudetteClearChatHistoryCommand, ClaudetteExportChatHistoryCommand, ClaudetteImportChatHistoryCommand

from .context.add_files import ClaudetteContextAddFilesCommand
from .context.add_current_file import ClaudetteContextAddCurrentFileCommand, ClaudetteContextRemoveCurrentFileCommand
from .context.add_open_files import ClaudetteContextAddOpenFilesCommand
from .context.clear_files import ClaudetteContextClearFilesCommand
from .context.manage_files import ClaudetteContextManageFilesCommand
from .context.refresh_files import ClaudetteContextRefreshFilesCommand

from .settings.select_model_panel import ClaudetteSelectModelPanelCommand
from .settings.select_system_message_panel import ClaudetteSelectSystemMessagePanelCommand

from .statusbar.spinner import ClaudetteSpinner

def plugin_loaded():
    spinner = ClaudetteSpinner()
    spinner.start("Claudette", 1000)

class ClaudetteFocusListener(sublime_plugin.EventListener):
    def on_activated(self, view):
        if view.settings().get('claudette_is_chat_view', False):
            self._update_current_chat_status(view)

    def on_load(self, view):
        if view.settings().get('claudette_is_chat_view', False):
            self._update_current_chat_status(view)

    def on_new(self, view):
        if view.settings().get('claudette_is_chat_view', False):
            self._update_current_chat_status(view)

    def on_clone(self, view):
        if view.settings().get('claudette_is_chat_view', False):
            self._update_current_chat_status(view)

    def _update_current_chat_status(self, view):
        window = view.window()
        if not window:
            print(f"No window found for view: {view.id()}")
            return

        view.settings().set('claudette_is_current_chat', True)

        for other_view in window.views():
            if (other_view.id() != view.id() and
                other_view.settings().get('claudette_is_chat_view', False)):
                other_view.settings().set('claudette_is_current_chat', False)
