class ClaudetteStreamingResponseHandler:
    def __init__(self, view, chat_view, on_complete=None):
        self.view = view
        self.chat_view = chat_view
        self.current_response = ""
        self.current_thinking = ""
        self.on_complete = on_complete
        self.is_completed = False
        self.in_thinking_block = False

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
            self._append_to_view(header)
            return

        if thinking_event == 'end':
            self.in_thinking_block = False
            self._append_to_view("\n\n---\n\n")
            return

        if is_thinking and self.in_thinking_block and chunk:
            self.current_thinking += chunk
            self._append_to_view(chunk)
            return

        # Handle regular text content
        if chunk:
            self.current_response += chunk
            self._append_to_view(chunk)

        if is_done:
            # Mark as completed to prevent duplicate handling
            self.is_completed = True
            # Let on_complete callback handle adding to conversation history
            if self.on_complete:
                self.on_complete()

    def _append_to_view(self, text):
        """Append text directly to the view."""
        self.view.set_read_only(False)
        self.view.run_command('append', {
            'characters': text,
            'force': True,
            'scroll_to_end': True
        })
        self.view.set_read_only(True)

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
                not self.is_completed):
                self.chat_view.handle_response(self.current_response, self.current_thinking)
                if self.on_complete:
                    self.on_complete()
        except:
            pass
