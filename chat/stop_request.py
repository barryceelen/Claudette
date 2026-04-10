import sublime
import sublime_plugin

from .chat_view import ClaudetteChatView


class ClaudetteStopRequestCommand(sublime_plugin.WindowCommand):
	"""Stop the current API request."""

	def run(self):
		"""Cancel the active request for the current chat view."""
		chat_view, view_id = self._get_chat_view_and_id()
		if not chat_view:
			sublime.status_message("No active Claudette chat")
			return

		if chat_view.cancel_request(view_id):
			sublime.status_message("Cancelling request...")
		else:
			sublime.status_message("No active request to cancel")

	def is_enabled(self):
		"""Only enabled when there's an active request in the focused chat."""
		chat_view, view_id = self._get_chat_view_and_id()
		return chat_view is not None and chat_view.has_active_request(view_id)

	def _get_chat_view_and_id(self):
		"""Get the ClaudetteChatView instance and focused chat view id."""
		window = self.window
		if not window:
			return None, None

		window_id = window.id()
		if window_id not in ClaudetteChatView._instances:
			return None, None

		chat_view = ClaudetteChatView._instances[window_id]

		# Determine which chat tab is focused so we cancel the right request
		active = window.active_view()
		if active and active.settings().get("claudette_is_chat_view", False):
			return chat_view, active.id()

		# Fall back to the manager's current view
		if chat_view.view:
			return chat_view, chat_view.view.id()

		return chat_view, None
