from __future__ import annotations

import math
import struct
import importlib.util
from pathlib import Path

import pytest


_MODULE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient/audio_opus.py"
_SPEC = importlib.util.spec_from_file_location("audio_opus", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
audio_opus = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audio_opus)


def _stereo_frame() -> bytes:
	values = []
	for index in range(audio_opus.FRAME_SAMPLES):
		left = round(12000 * math.sin(index * 2 * math.pi * 440 / audio_opus.SAMPLE_RATE))
		right = round(6000 * math.sin(index * 2 * math.pi * 880 / audio_opus.SAMPLE_RATE))
		values.extend((left, right))
	return struct.pack("<" + "h" * len(values), *values)


def _require_dll() -> None:
	if not audio_opus.is_available():
		pytest.skip("libopus DLLs have not been built")


def test_native_library_path_matches_process_architecture():
	expected = "x64" if __import__("ctypes").sizeof(__import__("ctypes").c_void_p) == 8 else "x86"
	assert audio_opus.native_library_path().parent.name == expected


def test_missing_dll_is_reported_without_import_failure(monkeypatch, tmp_path):
	monkeypatch.setattr(audio_opus, "_library", None)
	monkeypatch.setattr(audio_opus, "native_library_path", lambda: tmp_path / "missing" / "opus.dll")
	assert audio_opus.is_available() is False


def test_stereo_frame_round_trip_preserves_duration_and_channels():
	_require_dll()
	pcm = _stereo_frame()
	with audio_opus.OpusEncoder() as encoder, audio_opus.OpusDecoder() as decoder:
		packet = encoder.encode(pcm)
		decoded = decoder.decode(packet)
	assert len(pcm) == 3840
	assert len(decoded) == 3840
	samples = struct.unpack("<" + "h" * (len(decoded) // 2), decoded)
	left_energy = sum(abs(sample) for sample in samples[::2])
	right_energy = sum(abs(sample) for sample in samples[1::2])
	assert left_energy > 0
	assert right_energy > 0
	assert left_energy != right_energy


def test_pcm_frame_size_is_checked_before_native_call():
	_require_dll()
	with audio_opus.OpusEncoder() as encoder:
		with pytest.raises(ValueError):
			encoder.encode(b"\0" * (encoder.frame_bytes - 2))


def test_invalid_packet_is_reported_as_controlled_opus_error():
	_require_dll()
	with audio_opus.OpusDecoder() as decoder:
		with pytest.raises(audio_opus.OpusError) as error:
			decoder.decode(b"not an opus packet")
	assert error.value.code == audio_opus.OPUS_INVALID_PACKET


def test_close_is_idempotent_and_bitrate_can_change():
	_require_dll()
	encoder = audio_opus.OpusEncoder()
	encoder.set_bitrate(64000)
	encoder.close()
	encoder.close()
	decoder = audio_opus.OpusDecoder()
	decoder.set_gain_percent(0)
	decoder.set_gain_percent(80)
	decoder.set_gain_percent(100)
	decoder.close()
	decoder.close()
