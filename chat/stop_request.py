import sublime
import sublime_plugin

from ..constants import PLUGIN_NAME
from .chat_view import ClaudetteChatView


class ClaudetteStopRequestCommand(sublime_plugin.WindowCommand):
	"""Stop the current API request."""

	def run(self):
		"""Cancel the active request for the current chat view."""
		chat_view = self._get_chat_view()
		if not chat_view:
			sublime.status_message("No active Claudette chat")
			return

		if chat_view.cancel_request():
			sublime.status_message("Cancelling request...")
		else:
			sublime.status_message("No active request to cancel")

	def is_enabled(self):
		"""Only enabled when there's an active request."""
		chat_view = self._get_chat_view()
		return chat_view is not None and chat_view.has_active_request()

	def is_visible(self):
		"""Always visible in menus."""
		return True

	def _get_chat_view(self):
		"""Get the ClaudetteChatView instance for the current window."""
		window = self.window
		if not window:
			return None

		window_id = window.id()
		if window_id not in ClaudetteChatView._instances:
			return None

		return ClaudetteChatView._instances[window_id]
