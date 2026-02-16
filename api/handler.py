class ClaudetteStreamingResponseHandler:
    def __init__(self, view, on_complete=None, response_header_end=None):
        self.view = view
        self.on_complete = on_complete
        self.response_header_end = response_header_end
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

    def _insert_at_response_header(self, text):
        """Insert text immediately after # Claude's Response (before streamed content)."""
        if self.response_header_end is None or not text:
            return
        pos = self.response_header_end
        self.view.set_read_only(False)
        try:
            self.view.sel().clear()
            self.view.sel().add(sublime.Region(pos, pos))
            self.view.run_command('insert', {'characters': text})
            self.view.sel().clear()
            self.view.sel().add(sublime.Region(self.view.size(), self.view.size()))
        finally:
            self.view.set_read_only(True)

    def append_chunk(self, chunk, is_done=False, insert_after_response_header=False):
        if insert_after_response_header and self.response_header_end is not None:
            self._insert_at_response_header(chunk)
            return
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

        if is_done and self.on_complete:
            self.on_complete()
