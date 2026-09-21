"""Validation and negotiation helpers for the remote Opus protocol."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import time


CODEC_OPUS = "opus"
SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_MS = 20
MAX_PACKETS = 2
MAX_PACKET_BYTES = 4000
MAX_SEQUENCE = 2**32 - 1
MAX_STREAM_ID = 2**32 - 1
_MAX_BASE64_PACKET_LENGTH = 4 * ((MAX_PACKET_BYTES + 2) // 3)
_HALF_SEQUENCE_SPACE = 2**31

BITRATE_LEVELS = (48000, 64000, 96000, 128000)
INITIAL_BITRATE = 96000


class AudioProtocolError(ValueError):
	"""Raised when a remote audio negotiation or packet is invalid."""

	def __init__(self, reason: str, message: str):
		self.reason = reason
		super().__init__(message)


@dataclass(frozen=True)
class OpusSession:
	codec: str
	sample_rate: int
	channels: int
	frame_ms: int
	stream: int


@dataclass(frozen=True)
class OpusData:
	stream: int
	sequence: int
	frame_ms: int
	packets: tuple[bytes, ...]


class BitrateController:
	"""Choose stable Opus bitrate levels from transport pressure."""

	def __init__(
		self,
		initial=INITIAL_BITRATE,
		clock=None,
		min_change_interval=5.0,
		lower_observations=3,
		raise_observations=10,
	):
		if initial not in BITRATE_LEVELS:
			raise ValueError("unsupported initial bitrate")
		self.current = initial
		self._clock = clock or time.monotonic
		self._min_change_interval = min_change_interval
		self._lower_observations = lower_observations
		self._raise_observations = raise_observations
		self._last_change = float("-inf")
		self._candidate = None
		self._candidate_count = 0

	def observe(self, queue_depth, dropped=False, now=None):
		if type(queue_depth) is not int or queue_depth < 0:
			raise ValueError("queue depth must be a non-negative integer")
		now = self._clock() if now is None else now
		target = self._target(queue_depth, dropped)
		if target == self.current:
			self._candidate = None
			self._candidate_count = 0
			return None
		if target != self._candidate:
			self._candidate = target
			self._candidate_count = 0
		self._candidate_count += 1
		required = self._lower_observations if target < self.current else self._raise_observations
		if self._candidate_count < required or now - self._last_change < self._min_change_interval:
			return None
		self.current = target
		self._last_change = now
		self._candidate = None
		self._candidate_count = 0
		return self.current

	@staticmethod
	def _target(queue_depth, dropped):
		if dropped or queue_depth >= 8:
			return 48000
		if queue_depth >= 3:
			return 64000
		if queue_depth == 0:
			return 128000
		return 96000


def negotiate_opus(codecs, available: bool, sample_rate=None, channels=None, frame_ms=None) -> OpusSession:
	if type(codecs) is not list or CODEC_OPUS not in codecs:
		raise AudioProtocolError("unsupported_codec", "The peer did not offer Opus")
	if not available:
		raise AudioProtocolError("opus_unavailable", "Opus is not available on this computer")
	if sample_rate is not None and (sample_rate != SAMPLE_RATE or type(sample_rate) is not int):
		raise AudioProtocolError("invalid_parameters", "The peer selected an unsupported sample rate")
	if channels is not None and (channels != CHANNELS or type(channels) is not int):
		raise AudioProtocolError("invalid_parameters", "The peer selected an unsupported channel count")
	if frame_ms is not None and (frame_ms != FRAME_MS or type(frame_ms) is not int):
		raise AudioProtocolError("invalid_parameters", "The peer selected an unsupported frame duration")
	return OpusSession(CODEC_OPUS, SAMPLE_RATE, CHANNELS, FRAME_MS, 0)


def validate_response(codec, sample_rate, channels, frame_ms, stream) -> OpusSession:
	if codec != CODEC_OPUS:
		raise AudioProtocolError("unsupported_codec", "The peer selected an unsupported codec")
	if sample_rate != SAMPLE_RATE or type(sample_rate) is not int:
		raise AudioProtocolError("invalid_parameters", "The peer selected an unsupported sample rate")
	if channels != CHANNELS or type(channels) is not int:
		raise AudioProtocolError("invalid_parameters", "The peer selected an unsupported channel count")
	if frame_ms != FRAME_MS or type(frame_ms) is not int:
		raise AudioProtocolError("invalid_parameters", "The peer selected an unsupported frame duration")
	_validate_integer(stream, "stream", 1, MAX_STREAM_ID)
	return OpusSession(CODEC_OPUS, SAMPLE_RATE, CHANNELS, FRAME_MS, stream)


def validate_opus_data(
	stream,
	sequence,
	frame_ms,
	packets,
	expected_stream=None,
	sample_rate=None,
	channels=None,
) -> OpusData:
	_validate_integer(stream, "stream", 1, MAX_STREAM_ID)
	_validate_integer(sequence, "sequence", 0, MAX_SEQUENCE)
	if expected_stream is not None and stream != expected_stream:
		raise AudioProtocolError("wrong_stream", "The packet belongs to another audio stream")
	if frame_ms != FRAME_MS or type(frame_ms) is not int:
		raise AudioProtocolError("invalid_parameters", "The packet has an unsupported frame duration")
	if sample_rate is not None and (sample_rate != SAMPLE_RATE or type(sample_rate) is not int):
		raise AudioProtocolError("invalid_parameters", "The packet has an unsupported sample rate")
	if channels is not None and (channels != CHANNELS or type(channels) is not int):
		raise AudioProtocolError("invalid_parameters", "The packet has an unsupported channel count")
	if type(packets) is not list or not 1 <= len(packets) <= MAX_PACKETS:
		raise AudioProtocolError("invalid_packets", "An audio message must contain one or two packets")

	decoded = []
	for packet in packets:
		if not isinstance(packet, str) or not packet or len(packet) > _MAX_BASE64_PACKET_LENGTH:
			raise AudioProtocolError("invalid_packet", "The Base64 audio packet is too large or empty")
		try:
			value = base64.b64decode(packet, validate=True)
		except Exception as error:
			raise AudioProtocolError("invalid_packet", "The Base64 audio packet is invalid") from error
		if not 1 <= len(value) <= MAX_PACKET_BYTES:
			raise AudioProtocolError("invalid_packet", "The decoded audio packet is too large or empty")
		decoded.append(value)
	return OpusData(stream, sequence, FRAME_MS, tuple(decoded))


def _validate_integer(value, name: str, minimum: int, maximum: int) -> None:
	if type(value) is not int or not minimum <= value <= maximum:
		raise AudioProtocolError("invalid_parameters", f"Invalid {name}")


class SequenceTracker:
	"""Classify ordered packet groups without accepting duplicates or old data."""

	def __init__(self):
		self.expected = None
		self.missing_count = 0

	def reset(self) -> None:
		self.expected = None
		self.missing_count = 0

	def accept(self, sequence: int, packet_count: int) -> str:
		_validate_integer(sequence, "sequence", 0, MAX_SEQUENCE)
		_validate_integer(packet_count, "packet count", 1, MAX_PACKETS)
		self.missing_count = 0
		if self.expected is None:
			self.expected = _next_sequence(sequence, packet_count)
			return "accepted"
		distance = (sequence - self.expected) % (MAX_SEQUENCE + 1)
		if distance == 0:
			self.expected = _next_sequence(sequence, packet_count)
			return "accepted"
		if distance < _HALF_SEQUENCE_SPACE:
			self.missing_count = distance
			self.expected = _next_sequence(sequence, packet_count)
			return "missing"
		return "duplicate" if sequence == (self.expected - packet_count) % (MAX_SEQUENCE + 1) else "old"


def _next_sequence(sequence: int, packet_count: int) -> int:
	return (sequence + packet_count) % (MAX_SEQUENCE + 1)
