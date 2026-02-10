class ClaudetteStreamingResponseHandler:
    def __init__(self, view, chat_view, on_complete=None):
        self.view = view
        self.chat_view = chat_view
        self.current_response = ""
        self.on_complete = on_complete

    def append_chunk(self, chunk, is_done=False):
        if chunk:
            self.current_response += chunk
            self.view.set_read_only(False)
            self.view.run_command('append', {
                'characters': chunk,
                'force': True,
                'scroll_to_end': True
            })
            self.view.set_read_only(True)

        if is_done and self.on_complete:
            self.on_complete()
