"""Remote keyboard passthrough state and NVDA injection compatibility helpers."""

from contextlib import contextmanager


FEATURE_REMOTE_KEYBOARD_PASSTHROUGH = "remote_keyboard_passthrough"
MESSAGE_REQUEST = "remote_keyboard_passthrough_request"
MESSAGE_STATE = "remote_keyboard_passthrough_state"

MAX_REQUEST_ID_LENGTH = 128

ERROR_INVALID_ORIGIN = "invalid_origin"
ERROR_MULTIPLE_MASTERS = "multiple_masters"
ERROR_INVALID_REQUEST = "invalid_request"
ERROR_UNSUPPORTED_NVDA_VERSION = "unsupported_nvda_version"
ERROR_INTERNAL = "internal_error"
ERROR_NO_SLAVE = "no_slave"
ERROR_MULTIPLE_SLAVES = "multiple_slaves"
ERROR_REQUEST_PENDING = "request_pending"
ERROR_TIMEOUT = "timeout"


def validate_request(request_id, enabled):
	"""Return a stable error code for a network request, or ``None`` when valid."""
	if type(request_id) is not str or not request_id or len(request_id) > MAX_REQUEST_ID_LENGTH:
		return ERROR_INVALID_REQUEST
	if type(enabled) is not bool:
		return ERROR_INVALID_REQUEST
	return None


def is_available():
	"""Whether this NVDA exposes a safe way to ignore injected keyboard events."""
	try:
		import keyboardHandler
	except ImportError:
		return False
	return callable(getattr(keyboardHandler, "ignoreInjection", None)) or hasattr(
		keyboardHandler, "ignoreInjected"
	)


@contextmanager
def ignore_injection():
	"""Temporarily keep one injected keyboard event out of NVDA's gesture handler.

	Recent NVDA versions expose ``ignoreInjection`` as a context manager. Older
	versions which expose the backing flag are supported without touching the
	global keyboard hook itself. An unsupported version fails closed rather than
	pretending that the event bypassed NVDA.
	"""
	try:
		import keyboardHandler
	except ImportError as error:
		raise RuntimeError("NVDA keyboard injection bypass is unavailable") from error

	ignore_injection_context = getattr(keyboardHandler, "ignoreInjection", None)
	if callable(ignore_injection_context):
		with ignore_injection_context():
			yield
		return

	if not hasattr(keyboardHandler, "ignoreInjected"):
		raise RuntimeError("NVDA keyboard injection bypass is unavailable")
	previous = keyboardHandler.ignoreInjected
	keyboardHandler.ignoreInjected = True
	try:
		yield
	finally:
		keyboardHandler.ignoreInjected = previous


class RemoteKeyboardPassthrough:
	"""Session-only state for bypassing NVDA on remotely injected keys."""

	def __init__(self):
		self._enabled = False

	@property
	def enabled(self):
		return self._enabled

	def set_enabled(self, value):
		if type(value) is not bool:
			raise ValueError("Remote keyboard passthrough state must be a boolean")
		self._enabled = value

	def reset(self):
		self._enabled = False

	def inject(self, send_callable, *args, **kwargs):
		"""Call an injector, bypassing NVDA only while this state is enabled."""
		if not self.enabled:
			return send_callable(*args, **kwargs)
		with ignore_injection():
			return send_callable(*args, **kwargs)
