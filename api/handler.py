class ClaudetteStreamingResponseHandler:
    def __init__(self, view, chat_view=None, on_complete=None):
        self.view = view
        self.chat_view = chat_view
        self.on_complete = on_complete
        self.current_response = ""
        self.current_thinking = ""
        self.is_completed = False
        self.in_thinking_block = False
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

    def append_chunk(self, chunk, is_done=False, is_thinking=False, thinking_event=None):
        """Append a chunk to the view.

        Args:
            chunk: The text chunk to append
            is_done: Whether streaming is complete
            is_thinking: Whether this chunk is from thinking mode
            thinking_event: Special event - 'start' or 'end' for thinking block boundaries
        """
        if thinking_event == 'start':
            self.in_thinking_block = True
            header = "#### Thinking...\n\n"
            self._output_text(header)
            return

        if thinking_event == 'end':
            self.in_thinking_block = False
            self._output_text("\n\n---\n\n")
            return

        if is_thinking and self.in_thinking_block and chunk:
            self.current_thinking += chunk
            self._output_text(chunk)
            return

        # Handle regular text content with h1 to h2 conversion
        if chunk:
            self.current_response += chunk
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
            self.is_completed = True
            if self.on_complete:
                self.on_complete()

    def get_thinking_content(self):
        """Return the accumulated thinking content."""
        return self.current_thinking

    def get_response_content(self):
        """Return the accumulated response content."""
        return self.current_response

    def __del__(self):
        try:
            # Only handle response if streaming wasn't properly completed
            # This is a safety fallback for edge cases
            if (hasattr(self, 'current_response') and
                self.current_response and
                not self.is_completed and
                hasattr(self, 'chat_view') and
                self.chat_view):
                self.chat_view.handle_response(self.current_response, self.current_thinking)
                if self.on_complete:
                    self.on_complete()
        except Exception:
            pass
