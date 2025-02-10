import sublime
import time

class ClaudetteSpinner:
    def __init__(self):
        """Initialize the spinner with default values."""
        self.spinner_chars = [".  ", ".. ", "...", "   "]
        self.current_index = 0
        self.active = False
        self.message = ""
        self.timer = None
        self.start_time = None
        self.duration = None

    def start(self, message, duration=None):
        """
        Start the spinner with a given message.

        Args:
            message (str): The message to display with the spinner
            duration (int, optional): Duration in milliseconds after which the spinner should stop
        """
        self.message = message
        self.active = True
        self.current_index = 0  # Reset index when starting
        self.start_time = time.time()
        self.duration = duration
        self.update_spinner()

    def stop(self):
        """Stop the spinner and clean up."""
        self.active = False

        # Clear the timer if it exists
        if self.timer:
            sublime.set_timeout_async(self.timer, 0)
            self.timer = None

        # Ensure the status message is cleared
        sublime.set_timeout_async(lambda: sublime.status_message(""), 0)

        # Reset internal state
        self.message = ""
        self.current_index = 0
        self.start_time = None
        self.duration = None

    def update_spinner(self):
        """Update the spinner animation frame."""
        if not self.active:
            return

        # Check if duration has elapsed
        if self.duration is not None:
            elapsed_time = (time.time() - self.start_time) * 1000  # Convert to milliseconds
            if elapsed_time >= self.duration:
                self.stop()
                return

        # Update spinner frame
        spinner = self.spinner_chars[self.current_index]
        self.current_index = (self.current_index + 1) % len(self.spinner_chars)
        status = f"{self.message}{spinner}"

        # Update status message
        sublime.set_timeout_async(lambda: sublime.status_message(status), 0)

        # Schedule next update
        self.timer = sublime.set_timeout_async(self.update_spinner, 250)
