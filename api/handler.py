class ClaudetteStreamingResponseHandler:
    def __init__(self, view, chat_view, on_complete=None):
        self.view = view
        self.chat_view = chat_view
        self.current_response = ""
        self.on_complete = on_complete
        self.is_completed = False

    def append_chunk(self, chunk, is_done=False):
        self.current_response += chunk
        self.view.set_read_only(False)
        self.view.run_command('append', {
            'characters': chunk,
            'force': True,
            'scroll_to_end': True
        })
        self.view.set_read_only(True)

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
