import sublime

from ..utils import claudette_chat_status_message


class ClaudetteStreamingResponseHandler:
    def __init__(self, view, on_complete=None):
        self.view = view
        self.on_complete = on_complete
        self.line_buffer = ""
        self.at_line_start = True
        self._last_output_char = None
        self._deferred_chunks = []
        self._completed = False
        self._usage_info = None

    def _output_text(self, text):
        """Output text to the view."""
        if text:
            self.view.set_read_only(False)
            self.view.run_command(
                "append",
                {"characters": text, "force": True, "scroll_to_end": False},
            )
            self.view.set_read_only(True)
            self._last_output_char = text[-1]

    def _ensure_blank_line(self):
        """Pad the view so the next output starts after a blank line.

        Inspects the last two characters already in the view so the
        separator is correct regardless of whether the streamed text
        ended with a trailing newline.
        """
        size = self.view.size()
        if size == 0:
            return
        start = max(0, size - 2)
        tail = self.view.substr(sublime.Region(start, size))
        if tail.endswith("\n\n"):
            return
        if tail.endswith("\n"):
            self._output_text("\n")
        else:
            self._output_text("\n\n")

    def append_chunk(
        self,
        chunk,
        is_done=False,
        defer_to_end=False,
        was_cancelled=False,
        usage_info=None,
    ):
        if usage_info:
            self._usage_info = usage_info

        if defer_to_end and chunk:
            self._deferred_chunks.append(chunk)
            if self._completed:
                for deferred in self._deferred_chunks:
                    self._output_text(deferred)
                self._deferred_chunks = []
            return

        # Handle cancellation: flush buffer and show message
        if was_cancelled:
            if self.line_buffer:
                self._output_text(self.line_buffer)
                self.line_buffer = ""
            claudette_chat_status_message(
                self.view.window(), "Request cancelled", "❎"
            )
            self._completed = True
            if self.on_complete:
                self.on_complete()
            return

        # Process chunk character by character to convert h1 headings to h2,
        # keeping h1 reserved for user questions in the symbol list.
        for char in chunk:
            # Line break when a new sentence starts without a separator
            # (e.g. "results.Based" or "once!Based"). Checked per character
            # so it also catches cases that arrive inside a single chunk.
            if (
                char.isupper()
                and self._last_output_char is not None
                and self._last_output_char in ".!?"
                and not self.at_line_start
            ):
                self._output_text("\n")
                self.at_line_start = True

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

            if char == "\n":
                self.at_line_start = True

        # Flush buffer on completion
        if is_done and self.line_buffer:
            self._output_text(self.line_buffer)
            self.line_buffer = ""

        if is_done:
            # Output deferred content (e.g. the Sources block) after the
            # streamed answer, guaranteeing a blank-line separator so the
            # heading always renders as a paragraph of its own.
            if self._deferred_chunks:
                self._ensure_blank_line()
                for deferred in self._deferred_chunks:
                    self._output_text(deferred)
                self._deferred_chunks = []
            self._completed = True
            if self.on_complete:
                self.on_complete(usage_info=self._usage_info)
