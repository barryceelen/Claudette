import threading


class CancellationToken:
	"""Thread-safe cancellation token for API requests."""

	def __init__(self):
		self._cancelled = threading.Event()

	def cancel(self):
		"""Signal cancellation."""
		self._cancelled.set()

	def is_cancelled(self):
		"""Check if cancellation was requested."""
		return self._cancelled.is_set()

	def reset(self):
		"""Reset for reuse."""
		self._cancelled.clear()
