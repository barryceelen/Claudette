class ClaudetteStreamingResponseHandler:
    def __init__(self, view, chat_view, on_complete=None):
        self.view = view
        self.chat_view = chat_view
        self.current_response = ""
        self.on_complete = on_complete
        self.is_completed = False
        self.line_buffer = ""
        self.at_line_start = True

    def _output_text(self, text):
        """Output text to the view."""
        if text:
            self.view.set_read_only(False)
            self.view.run_command('append', {
                'characters': text,
                'force': True,
                'scroll_to_end': False
            })
            self.view.set_read_only(True)

    def append_chunk(self, chunk, is_done=False):
        self.current_response += chunk

        # Process chunk character by character to convert h1 headings to h2,
        # keeping h1 reserved for user questions in the symbol list.
        for char in chunk:
            if self.at_line_start:
                self.line_buffer += char
                if self.line_buffer == "# ":
                    # Convert h1 to h2
                    self._output_text("## ")
                    self.line_buffer = ""
                    self.at_line_start = False
                elif self.line_buffer == "#":
                    # Could be h1, keep buffering
                    pass
                elif self.line_buffer.startswith("##"):
                    # Already h2 or lower, output and continue
                    self._output_text(self.line_buffer)
                    self.line_buffer = ""
                    self.at_line_start = False
                elif not "# ".startswith(self.line_buffer):
                    # Not going to be an h1, output buffer
                    self._output_text(self.line_buffer)
                    self.line_buffer = ""
                    self.at_line_start = False
            else:
                self._output_text(char)

            if char == '\n':
                self.at_line_start = True

        # Flush buffer on completion
        if is_done and self.line_buffer:
            self._output_text(self.line_buffer)
            self.line_buffer = ""

        if is_done:
            # Mark as completed to prevent duplicate handling
            self.is_completed = True
            # Let on_complete callback handle adding to conversation history
            if self.on_complete:
                self.on_complete()

    def __del__(self):
        try:
            # Only handle response if streaming wasn't properly completed
            # This is a safety fallback for edge cases
            if (hasattr(self, 'current_response') and
                self.current_response and
                not self.is_completed):
                self.chat_view.handle_response(self.current_response)
                if self.on_complete:
                    self.on_complete()
        except:
            pass
