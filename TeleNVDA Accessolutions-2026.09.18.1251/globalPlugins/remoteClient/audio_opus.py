"""Small ctypes wrapper for the bundled libopus build."""

from __future__ import annotations

import ctypes
import math
from pathlib import Path
from typing import Any


SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
BITS_PER_SAMPLE = 16
MAX_PACKET_BYTES = 4000

OPUS_APPLICATION_AUDIO = 2049
OPUS_AUTO = -1000

OPUS_BAD_ARG = -1
OPUS_BUFFER_TOO_SMALL = -2
OPUS_INTERNAL_ERROR = -3
OPUS_INVALID_PACKET = -4
OPUS_UNIMPLEMENTED = -5
OPUS_INVALID_STATE = -6
OPUS_ALLOC_FAIL = -7

OPUS_SET_BITRATE_REQUEST = 4002
OPUS_SET_VBR_REQUEST = 4006
OPUS_SET_COMPLEXITY_REQUEST = 4010
OPUS_SET_SIGNAL_REQUEST = 4024
OPUS_SET_GAIN_REQUEST = 4034
OPUS_RESET_STATE_REQUEST = 4028

_ERROR_NAMES = {
	OPUS_BAD_ARG: "bad argument",
	OPUS_BUFFER_TOO_SMALL: "buffer too small",
	OPUS_INTERNAL_ERROR: "internal error",
	OPUS_INVALID_PACKET: "invalid packet",
	OPUS_UNIMPLEMENTED: "unimplemented",
	OPUS_INVALID_STATE: "invalid state",
	OPUS_ALLOC_FAIL: "allocation failed",
}

_library: Any = None


class OpusUnavailableError(RuntimeError):
	"""Raised when the architecture-specific Opus DLL cannot be loaded."""


class OpusError(RuntimeError):
	"""Raised for a non-zero libopus error code."""

	def __init__(self, code: int, message: str | None = None):
		self.code = code
		name = _ERROR_NAMES.get(code, "unknown error")
		super().__init__(f"libopus error {code} ({name}){': ' + message if message else ''}")


def native_directory() -> Path:
	"""Return the native directory matching this Python process."""

	architecture = "x64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "x86"
	return Path(__file__).resolve().parent / "native" / architecture


def native_library_path() -> Path:
	return native_directory() / "opus.dll"


def _configure_library(library: Any) -> Any:
	library.opus_get_version_string.argtypes = []
	library.opus_get_version_string.restype = ctypes.c_char_p
	library.opus_strerror.argtypes = [ctypes.c_int]
	library.opus_strerror.restype = ctypes.c_char_p

	library.opus_encoder_create.argtypes = [
		ctypes.c_int,
		ctypes.c_int,
		ctypes.c_int,
		ctypes.POINTER(ctypes.c_int),
	]
	library.opus_encoder_create.restype = ctypes.c_void_p
	library.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
	library.opus_encoder_destroy.restype = None
	library.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int]
	library.opus_encoder_ctl.restype = ctypes.c_int
	library.opus_encode.argtypes = [
		ctypes.c_void_p,
		ctypes.POINTER(ctypes.c_int16),
		ctypes.c_int,
		ctypes.POINTER(ctypes.c_ubyte),
		ctypes.c_int,
	]
	library.opus_encode.restype = ctypes.c_int

	library.opus_decoder_create.argtypes = [
		ctypes.c_int,
		ctypes.c_int,
		ctypes.POINTER(ctypes.c_int),
	]
	library.opus_decoder_create.restype = ctypes.c_void_p
	library.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
	library.opus_decoder_destroy.restype = None
	library.opus_decoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int]
	library.opus_decoder_ctl.restype = ctypes.c_int
	library.opus_decode.argtypes = [
		ctypes.c_void_p,
		ctypes.POINTER(ctypes.c_ubyte),
		ctypes.c_int,
		ctypes.POINTER(ctypes.c_int16),
		ctypes.c_int,
		ctypes.c_int,
	]
	library.opus_decode.restype = ctypes.c_int
	return library


def _load_library() -> Any:
	global _library
	if _library is not None:
		return _library
	path = native_library_path()
	if not path.is_file():
		raise OpusUnavailableError(f"Opus DLL not found: {path}")
	try:
		_library = _configure_library(ctypes.CDLL(str(path)))
	except (OSError, AttributeError) as error:
		raise OpusUnavailableError(f"Unable to load Opus DLL {path}: {error}") from error
	return _library


def is_available() -> bool:
	try:
		with OpusEncoder(), OpusDecoder():
			pass
	except (OpusError, OpusUnavailableError, OSError):
		return False
	return True


def version() -> str:
	library = _load_library()
	value = library.opus_get_version_string()
	return value.decode("ascii") if value else "unknown"


def _native_message(library: Any, code: int) -> str | None:
	try:
		value = library.opus_strerror(code)
	except Exception:
		return None
	return value.decode("ascii", errors="replace") if value else None


def _check_result(library: Any, code: int) -> None:
	if code < 0:
		raise OpusError(code, _native_message(library, code))


def _pcm_bytes(pcm: bytes | bytearray | memoryview, expected: int) -> bytes:
	data = bytes(pcm)
	if len(data) != expected:
		raise ValueError(f"PCM frame must contain exactly {expected} bytes, got {len(data)}")
	return data


def _gain_for_percent(percent: int) -> int:
	if not 0 <= percent <= 100:
		raise ValueError("gain must be between 0 and 100 percent")
	if percent == 0:
		return -32768
	return round(20 * math.log10(percent / 100) * 256)


class OpusEncoder:
	"""One owner for one native Opus encoder."""

	def __init__(
		self,
		sample_rate: int = SAMPLE_RATE,
		channels: int = CHANNELS,
		bitrate: int = 96000,
		complexity: int = 6,
	):
		if sample_rate != SAMPLE_RATE or channels not in (1, 2):
			raise ValueError("phase 1 supports 48 kHz mono or stereo only")
		if bitrate <= 0:
			raise ValueError("bitrate must be positive")
		if not 0 <= complexity <= 10:
			raise ValueError("complexity must be between 0 and 10")
		self.sample_rate = sample_rate
		self.channels = channels
		self.frame_samples = FRAME_SAMPLES
		self.frame_bytes = FRAME_SAMPLES * channels * 2
		self._library = _load_library()
		self._handle: Any = None
		error = ctypes.c_int(0)
		handle = self._library.opus_encoder_create(
			sample_rate, channels, OPUS_APPLICATION_AUDIO, ctypes.byref(error)
		)
		if not handle:
			_check_result(self._library, error.value or OPUS_ALLOC_FAIL)
		self._handle = handle
		try:
			self._ctl(OPUS_SET_VBR_REQUEST, 1)
			self._ctl(OPUS_SET_SIGNAL_REQUEST, OPUS_AUTO)
			self._ctl(OPUS_SET_BITRATE_REQUEST, bitrate)
			self._ctl(OPUS_SET_COMPLEXITY_REQUEST, complexity)
		except Exception:
			self.close()
			raise

	def _ctl(self, request: int, value: int) -> None:
		if self._handle is None:
			raise OpusError(OPUS_INVALID_STATE)
		_check_result(self._library, self._library.opus_encoder_ctl(self._handle, request, value))

	def set_bitrate(self, bitrate: int) -> None:
		if bitrate <= 0:
			raise ValueError("bitrate must be positive")
		self._ctl(OPUS_SET_BITRATE_REQUEST, bitrate)

	def encode(self, pcm: bytes | bytearray | memoryview) -> bytes:
		data = _pcm_bytes(pcm, self.frame_bytes)
		if self._handle is None:
			raise OpusError(OPUS_INVALID_STATE)
		input_type = ctypes.c_int16 * (self.frame_bytes // 2)
		output_type = ctypes.c_ubyte * MAX_PACKET_BYTES
		input_buffer = input_type.from_buffer_copy(data)
		output_buffer = output_type()
		result = self._library.opus_encode(
			self._handle,
			input_buffer,
			self.frame_samples,
			output_buffer,
			MAX_PACKET_BYTES,
		)
		_check_result(self._library, result)
		return bytes(output_buffer[:result])

	def close(self) -> None:
		if self._handle is None:
			return
		handle, self._handle = self._handle, None
		self._library.opus_encoder_destroy(handle)

	def __enter__(self) -> OpusEncoder:
		return self

	def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
		self.close()

	def __del__(self):
		try:
			self.close()
		except Exception:
			pass


class OpusDecoder:
	"""One owner for one native Opus decoder."""

	def __init__(self, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS):
		if sample_rate != SAMPLE_RATE or channels not in (1, 2):
			raise ValueError("phase 1 supports 48 kHz mono or stereo only")
		self.sample_rate = sample_rate
		self.channels = channels
		self.frame_samples = FRAME_SAMPLES
		self.frame_bytes = FRAME_SAMPLES * channels * 2
		self._library = _load_library()
		self._handle: Any = None
		error = ctypes.c_int(0)
		handle = self._library.opus_decoder_create(sample_rate, channels, ctypes.byref(error))
		if not handle:
			_check_result(self._library, error.value or OPUS_ALLOC_FAIL)
		self._handle = handle

	def set_gain_percent(self, percent: int) -> None:
		if self._handle is None:
			raise OpusError(OPUS_INVALID_STATE)
		gain = _gain_for_percent(percent)
		_check_result(self._library, self._library.opus_decoder_ctl(self._handle, OPUS_SET_GAIN_REQUEST, gain))

	def reset(self) -> None:
		if self._handle is None:
			raise OpusError(OPUS_INVALID_STATE)
		_check_result(self._library, self._library.opus_decoder_ctl(self._handle, OPUS_RESET_STATE_REQUEST))

	def decode(self, packet: bytes | bytearray | memoryview | None) -> bytes:
		data = None if packet is None else bytes(packet)
		if data is not None and (not data or len(data) > MAX_PACKET_BYTES):
			raise ValueError("Opus packet must be between 1 and 4000 bytes")
		if self._handle is None:
			raise OpusError(OPUS_INVALID_STATE)
		packet_buffer = None
		packet_length = 0
		if data is not None:
			packet_type = ctypes.c_ubyte * len(data)
			packet_buffer = packet_type.from_buffer_copy(data)
			packet_length = len(data)
		output_type = ctypes.c_int16 * (self.frame_samples * self.channels)
		output_buffer = output_type()
		result = self._library.opus_decode(
			self._handle,
			packet_buffer,
			packet_length,
			output_buffer,
			self.frame_samples,
			0,
		)
		_check_result(self._library, result)
		output_bytes = result * self.channels * ctypes.sizeof(ctypes.c_int16)
		return ctypes.string_at(ctypes.addressof(output_buffer), output_bytes)

	def decode_lost(self) -> bytes:
		return self.decode(None)

	def close(self) -> None:
		if self._handle is None:
			return
		handle, self._handle = self._handle, None
		self._library.opus_decoder_destroy(handle)

	def __enter__(self) -> OpusDecoder:
		return self

	def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
		self.close()

	def __del__(self):
		try:
			self.close()
		except Exception:
			pass
