"""Pure protocol helpers for the HTTPS long-polling transport."""

import base64
from dataclasses import dataclass
import json
import re
from typing import Any, Iterable, Mapping


DEFAULT_PATH = "/remote/http/v1"
PROTOCOL = "nvdaremote-http/1"
DEFAULT_PATH_V2 = "/remote/http/v2"
PROTOCOL_V2 = "nvdaremote-http/2"
MIN_SESSION_ID_BYTES = 16
SESSION_TOKEN_BYTES = 32
MAX_BATCH_MESSAGES = 32
DEFAULT_STREAM_KEEPALIVE = 15
MAX_STREAM_LINE_BYTES = 1 << 20

_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PATH_ALLOWED = frozenset("/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@%")


class HTTPSProtocolError(ValueError):
	def __init__(self, message: str, reason: str = "invalid_protocol"):
		super().__init__(message)
		self.reason = reason


class HTTPSAuthenticationError(HTTPSProtocolError):
	def __init__(self, message: str = "HTTPS authentication failed"):
		super().__init__(message, reason="authentication_failed")


class HTTPSSequenceError(HTTPSProtocolError):
	def __init__(self, expected: int, received: int):
		super().__init__(
			f"Expected message sequence {expected}, received {received}",
			reason="sequence_gap",
		)
		self.expected = expected
		self.received = received


@dataclass(frozen=True)
class MessageEnvelope:
	sequence: int
	payload: dict[str, Any]

	def __post_init__(self) -> None:
		_validate_sequence_number(self.sequence)
		if not isinstance(self.payload, dict):
			raise HTTPSProtocolError("Message payload must be a JSON object", reason="invalid_message")


@dataclass(frozen=True)
class SessionResponse:
	session_id: str
	token: str
	protocol: str
	poll_wait_seconds: int
	max_batch_messages: int
	max_message_bytes: int
	stream_keepalive_seconds: int = DEFAULT_STREAM_KEEPALIVE


@dataclass(frozen=True)
class PollResponse:
	messages: tuple[MessageEnvelope, ...]
	last_sequence: int


def normalize_https_path(path: str | None) -> str:
	if path is None:
		return DEFAULT_PATH
	if not isinstance(path, str):
		raise HTTPSProtocolError("HTTPS path must be a string", reason="invalid_path")
	path = path.strip()
	if not path:
		return DEFAULT_PATH
	if any(character not in _PATH_ALLOWED for character in path):
		raise HTTPSProtocolError("HTTPS path contains an invalid character", reason="invalid_path")
	if "%" in path:
		for match in re.finditer("%", path):
			position = match.start()
			if position + 2 >= len(path) or not re.fullmatch(
				r"[0-9A-Fa-f]{2}",
				path[position + 1 : position + 3],
			):
				raise HTTPSProtocolError("HTTPS path contains an invalid escape", reason="invalid_path")
	if not path.startswith("/"):
		path = "/" + path
	if len(path) > 1:
		path = path.rstrip("/")
	return path


def https_url(host: str, port: int, path: str | None = None) -> str:
	if not isinstance(host, str) or not host or any(character in host for character in "/?#\r\n"):
		raise HTTPSProtocolError("HTTPS host is invalid", reason="invalid_address")
	if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
		raise HTTPSProtocolError("HTTPS port is invalid", reason="invalid_address")
	host = host.strip()
	if host.startswith("[") and host.endswith("]"):
		host = host[1:-1]
	if ":" in host:
		host = f"[{host}]"
	return f"https://{host}:{port}{normalize_https_path(path)}"


def sessions_path(base_path: str | None = None) -> str:
	return f"{normalize_https_path(base_path)}/sessions"


def validate_session_id(session_id: str) -> str:
	if not isinstance(session_id, str) or not session_id or not _BASE64URL_RE.fullmatch(session_id):
		raise HTTPSProtocolError("Session identifier is invalid", reason="invalid_session_id")
	try:
		decoded = base64.urlsafe_b64decode(session_id + "=" * (-len(session_id) % 4))
	except (ValueError, TypeError):
		raise HTTPSProtocolError("Session identifier is invalid", reason="invalid_session_id") from None
	if (
		len(decoded) < MIN_SESSION_ID_BYTES
		or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != session_id
	):
		raise HTTPSProtocolError("Session identifier is invalid", reason="invalid_session_id")
	return session_id


def validate_session_token(token: str) -> str:
	if not isinstance(token, str) or not token or not _BASE64URL_RE.fullmatch(token):
		raise HTTPSAuthenticationError("Session token is invalid")
	try:
		decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
	except (ValueError, TypeError):
		raise HTTPSAuthenticationError("Session token is invalid") from None
	if (
		len(decoded) != SESSION_TOKEN_BYTES
		or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != token
	):
		raise HTTPSAuthenticationError("Session token is invalid")
	return token


def session_path(base_path: str | None, session_id: str) -> str:
	return f"{sessions_path(base_path)}/{validate_session_id(session_id)}"


def messages_path(base_path: str | None, session_id: str) -> str:
	return f"{session_path(base_path, session_id)}/messages"


def stream_path(base_path: str | None, session_id: str) -> str:
	return f"{session_path(base_path, session_id)}/stream"


def encode_session_request(protocol: str = PROTOCOL) -> bytes:
	return _encode_json({"protocol": protocol})


def decode_session_response(raw: bytes | str) -> SessionResponse:
	value = _decode_json_object(raw)
	try:
		session_id = validate_session_id(value["sessionId"])
		token = validate_session_token(value["token"])
		protocol = value["protocol"]
		poll_wait_seconds = value["pollWaitSeconds"]
		max_batch_messages = value["maxBatchMessages"]
		max_message_bytes = value["maxMessageBytes"]
	except KeyError as error:
		raise HTTPSProtocolError(
			f"Session response is missing {error.args[0]}",
			reason="invalid_session_response",
		) from None
	if protocol not in (PROTOCOL, PROTOCOL_V2):
		raise HTTPSProtocolError("Unsupported HTTPS relay protocol", reason="unsupported_protocol")
	_validate_positive_integer(poll_wait_seconds, "pollWaitSeconds")
	_validate_positive_integer(max_batch_messages, "maxBatchMessages")
	_validate_positive_integer(max_message_bytes, "maxMessageBytes")
	stream_keepalive_seconds = value.get("streamKeepaliveSeconds", DEFAULT_STREAM_KEEPALIVE)
	_validate_positive_integer(stream_keepalive_seconds, "streamKeepaliveSeconds")
	return SessionResponse(
		session_id=session_id,
		token=token,
		protocol=protocol,
		poll_wait_seconds=poll_wait_seconds,
		max_batch_messages=max_batch_messages,
		max_message_bytes=max_message_bytes,
		stream_keepalive_seconds=stream_keepalive_seconds,
	)


def encode_message_batch(messages: Iterable[MessageEnvelope], received_through: int = 0) -> bytes:
	items = tuple(messages)
	if not isinstance(received_through, int) or isinstance(received_through, bool) or received_through < 0:
		raise HTTPSProtocolError("Received sequence is invalid", reason="invalid_sequence")
	if not items and received_through == 0:
		raise HTTPSProtocolError("Message batch size is invalid", reason="invalid_batch")
	if len(items) > MAX_BATCH_MESSAGES:
		raise HTTPSProtocolError("Message batch size is invalid", reason="invalid_batch")
	previous = 0
	encoded_messages: list[dict[str, Any]] = []
	for message in items:
		if not isinstance(message, MessageEnvelope):
			raise HTTPSProtocolError("Message batch contains an invalid envelope", reason="invalid_message")
		if message.sequence <= previous:
			raise HTTPSSequenceError(previous + 1, message.sequence)
		previous = message.sequence
		encoded_messages.append({"sequence": message.sequence, "payload": message.payload})
	envelope: dict[str, Any] = {"messages": encoded_messages}
	if received_through > 0:
		envelope["receivedThrough"] = received_through
	return _encode_json(envelope)


def decode_ack_response(raw: bytes | str) -> int:
	value = _decode_json_object(raw)
	accepted_through = value.get("acceptedThrough")
	_validate_positive_integer(accepted_through, "acceptedThrough")
	return accepted_through


def decode_poll_response(raw: bytes | str) -> PollResponse:
	value = _decode_json_object(raw)
	messages_value = value.get("messages")
	last_sequence = value.get("lastSequence")
	if not isinstance(messages_value, list) or len(messages_value) > MAX_BATCH_MESSAGES:
		raise HTTPSProtocolError("Poll response messages are invalid", reason="invalid_poll_response")
	if not isinstance(last_sequence, int) or isinstance(last_sequence, bool) or last_sequence < 0:
		raise HTTPSProtocolError("Poll response lastSequence is invalid", reason="invalid_poll_response")
	messages = tuple(_decode_message(message) for message in messages_value)
	previous = 0
	for message in messages:
		if message.sequence <= previous:
			raise HTTPSSequenceError(previous + 1, message.sequence)
		previous = message.sequence
	if messages and last_sequence < messages[-1].sequence:
		raise HTTPSProtocolError(
			"Poll response lastSequence is behind its messages",
			reason="invalid_poll_response",
		)
	return PollResponse(messages=messages, last_sequence=last_sequence)


class SequenceTracker:
	def __init__(self, last_sequence: int = 0) -> None:
		if not isinstance(last_sequence, int) or isinstance(last_sequence, bool) or last_sequence < 0:
			raise HTTPSProtocolError("Initial sequence is invalid", reason="invalid_sequence")
		self.last_sequence = last_sequence

	def accept(self, sequence: int) -> bool:
		_validate_sequence_number(sequence)
		if sequence <= self.last_sequence:
			return False
		expected = self.last_sequence + 1
		if sequence != expected:
			raise HTTPSSequenceError(expected, sequence)
		self.last_sequence = sequence
		return True


def _validate_sequence_number(sequence: Any) -> None:
	if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
		raise HTTPSProtocolError("Message sequence is invalid", reason="invalid_sequence")


def _validate_positive_integer(value: Any, field: str) -> None:
	if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
		raise HTTPSProtocolError(f"{field} must be a positive integer", reason="invalid_protocol")


def _decode_message(value: Any) -> MessageEnvelope:
	if not isinstance(value, dict) or set(value) != {"sequence", "payload"}:
		raise HTTPSProtocolError("Message envelope is invalid", reason="invalid_message")
	return MessageEnvelope(value["sequence"], value["payload"])


def _encode_json(value: Mapping[str, Any]) -> bytes:
	try:
		return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
	except (TypeError, ValueError) as error:
		raise HTTPSProtocolError("JSON envelope cannot be encoded", reason="invalid_json") from error


def _decode_json_object(raw: bytes | str) -> dict[str, Any]:
	try:
		if isinstance(raw, bytes):
			raw = raw.decode("utf-8")
		if not isinstance(raw, str):
			raise TypeError
		value = json.loads(raw, parse_constant=_reject_json_constant)
	except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as error:
		raise HTTPSProtocolError("JSON envelope is invalid", reason="invalid_json") from error
	if not isinstance(value, dict):
		raise HTTPSProtocolError("JSON envelope must be an object", reason="invalid_json")
	return value


def _reject_json_constant(value: str) -> None:
	raise ValueError(f"Invalid JSON constant: {value}")
