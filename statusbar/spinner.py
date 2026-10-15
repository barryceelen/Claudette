import time

import sublime

from ..constants import SPINNER_CHARS, SPINNER_INTERVAL_MS

STATUS_KEY = "claudette_spinner"
DEFAULT_STATUS_CLEAR_MS = 5000


def show_view_status(view, message, clear_after_ms=DEFAULT_STATUS_CLEAR_MS):
    """
    Show a temporary keyed status on a view.

    Uses the same key as the spinner so request progress and the final
    cost/result message share one slot on that chat view only.
    """

    def apply():
        if not view or not view.is_valid():
            return

        view.set_status(STATUS_KEY, message)

        if clear_after_ms is None:
            return

        def clear():
            if (
                view.is_valid()
                and view.get_status(STATUS_KEY) == message
            ):
                view.erase_status(STATUS_KEY)

        sublime.set_timeout(clear, clear_after_ms)

    sublime.set_timeout(apply, 0)


class ClaudetteSpinner:
    def __init__(self):
        """Initialize the spinner with default values."""
        self.current_index = 0
        self.active = False
        self.message = ""
        self.start_time = None
        self.duration = None
        self._view = None

    def set_message(self, message):
        """Update the spinner message without stopping."""
        self.message = message

    def start(self, message, duration=None, view=None):
        """
        Start the spinner with a given message.

        Args:
            message (str): Message shown with the spinner
            duration (int, optional): Stop after this many milliseconds
            view (sublime.View, optional): Chat view that owns this request
        """
        self.message = message
        self.active = True
        self.current_index = 0  # Reset index when starting
        self.start_time = time.time()
        self.duration = duration
        self._view = view
        self.update_spinner()

    def stop(self):
        """Stop the spinner and clean up."""
        self.active = False
        sublime.set_timeout(self._erase_status, 0)

        self.message = ""
        self.current_index = 0
        self.start_time = None
        self.duration = None

    def _erase_status(self):
        """Remove the spinner key from the view that owns this request."""
        if self.active:
            return

        view = self._view
        self._view = None
        if view and view.is_valid():
            view.erase_status(STATUS_KEY)

    def _show_status(self, status):
        """Show one frame on the request's chat view."""
        if not self.active:
            return

        view = self._view
        if not view or not view.is_valid():
            return

        view.set_status(STATUS_KEY, status)

    def update_spinner(self):
        """Update the spinner animation frame."""
        if not self.active:
            return

        if self.duration is not None and self.start_time is not None:
            elapsed_time = (
                time.time() - self.start_time
            ) * 1000  # Convert to milliseconds
            if elapsed_time >= self.duration:
                self.stop()
                return

        spinner = SPINNER_CHARS[self.current_index]
        self.current_index = (self.current_index + 1) % len(SPINNER_CHARS)
        status = f"{self.message} {spinner}"

        sublime.set_timeout(lambda s=status: self._show_status(s), 0)
        sublime.set_timeout_async(self.update_spinner, SPINNER_INTERVAL_MS)
