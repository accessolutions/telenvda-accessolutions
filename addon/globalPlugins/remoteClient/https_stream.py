"""Minimal Server-Sent Events reader for the HTTPS streaming transport."""

import json
from typing import NamedTuple, Protocol

from . import https_protocol


class HTTPSStreamClosed(Exception):
	"""The server closed the streaming response."""


class StreamEvent(NamedTuple):
	event: str
	data: str
	sequence: int | None


class _LineReader(Protocol):
	def readline(self, size: int = -1) -> bytes:
		...


def read_event(response: _LineReader, max_data_bytes: int) -> StreamEvent | None:
	"""Read one SSE block from a buffered byte response.

	Returns None for a keepalive or comment-only block and raises
	HTTPSStreamClosed when the response ends before a complete block.
	"""
	event_type = "message"
	sequence = None
	data_parts: list[str] = []
	data_size = 0
	while True:
		line = response.readline(https_protocol.MAX_STREAM_LINE_BYTES + 1)
		if not line:
			raise HTTPSStreamClosed("The HTTPS stream ended")
		if len(line) > https_protocol.MAX_STREAM_LINE_BYTES:
			raise https_protocol.HTTPSProtocolError("Stream line is too long", reason="invalid_stream")
		line = line.rstrip(b"\r\n")
		if not line:
			if not data_parts:
				return None
			return StreamEvent(event_type, "\n".join(data_parts), sequence)
		if line.startswith(b":"):
			continue
		parts = line.partition(b":")
		field = parts[0]
		value = parts[2]
		if value.startswith(b" "):
			value = value[1:]
		try:
			field_name = field.decode("ascii")
			decoded = value.decode("utf-8")
		except UnicodeDecodeError as error:
			raise https_protocol.HTTPSProtocolError(
				"Stream field is not valid UTF-8",
				reason="invalid_stream",
			) from error
		if field_name == "event":
			event_type = decoded
		elif field_name == "id":
			try:
				sequence = int(decoded)
			except ValueError as error:
				raise https_protocol.HTTPSProtocolError("Stream id is invalid", reason="invalid_sequence") from error
			if sequence <= 0:
				raise https_protocol.HTTPSProtocolError("Stream id is invalid", reason="invalid_sequence")
		elif field_name == "data":
			data_size += len(value)
			if data_size > max_data_bytes:
				raise https_protocol.HTTPSProtocolError("Stream payload is too large", reason="invalid_stream")
			data_parts.append(decoded)


def decode_event_payload(event: StreamEvent) -> dict:
	try:
		payload = json.loads(event.data)
	except ValueError as error:
		raise https_protocol.HTTPSProtocolError("Stream payload is not valid JSON", reason="invalid_json") from error
	if not isinstance(payload, dict):
		raise https_protocol.HTTPSProtocolError("Stream payload must be a JSON object", reason="invalid_message")
	return payload
