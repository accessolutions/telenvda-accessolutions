import base64
import importlib.util
from pathlib import Path
import sys

import pytest


MODULE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient/audio_protocol.py"
SPEC = importlib.util.spec_from_file_location("audio_protocol", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
audio_protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audio_protocol
SPEC.loader.exec_module(audio_protocol)


def _packet(value=b"opus"):
	return base64.b64encode(value).decode("ascii")


def test_opus_is_selected_only_when_offered_and_available():
	parameters = audio_protocol.negotiate_opus([audio_protocol.CODEC_OPUS], available=True)

	assert parameters.codec == audio_protocol.CODEC_OPUS
	assert parameters.sample_rate == 48000
	assert parameters.channels == 2
	assert parameters.frame_ms == 20

	with pytest.raises(audio_protocol.AudioProtocolError) as unavailable:
		audio_protocol.negotiate_opus([audio_protocol.CODEC_OPUS], available=False)
	assert unavailable.value.reason == "opus_unavailable"

	with pytest.raises(audio_protocol.AudioProtocolError) as unsupported:
		audio_protocol.negotiate_opus(["pcm"], available=True)
	assert unsupported.value.reason == "unsupported_codec"


def test_response_must_confirm_the_complete_opus_format():
	parameters = audio_protocol.validate_response("opus", 48000, 2, 20, 12)

	assert parameters.stream == 12
	for field, value in (("codec", "pcm"), ("sample_rate", 16000), ("channels", 1), ("frame_ms", 40), ("stream", 0)):
		values = {"codec": "opus", "sample_rate": 48000, "channels": 2, "frame_ms": 20, "stream": 12}
		values[field] = value
		with pytest.raises(audio_protocol.AudioProtocolError):
			audio_protocol.validate_response(**values)


def test_opus_data_validates_stream_size_and_packet_count():
	data = audio_protocol.validate_opus_data(12, 100, 20, [_packet(b"one"), _packet(b"two")], expected_stream=12)

	assert data.sequence == 100
	assert data.packets == (b"one", b"two")

	with pytest.raises(audio_protocol.AudioProtocolError):
		audio_protocol.validate_opus_data(13, 100, 20, [_packet()], expected_stream=12)
	with pytest.raises(audio_protocol.AudioProtocolError):
		audio_protocol.validate_opus_data(12, 100, 20, [_packet()] * 3)
	with pytest.raises(audio_protocol.AudioProtocolError):
		audio_protocol.validate_opus_data(12, 100, 20, ["not base64"])


def test_sequence_tracker_distinguishes_missing_duplicate_and_old_groups():
	tracker = audio_protocol.SequenceTracker()

	assert tracker.accept(10, 1) == "accepted"
	assert tracker.accept(12, 1) == "missing"
	assert tracker.missing_count == 1
	assert tracker.accept(12, 1) == "duplicate"
	assert tracker.accept(8, 1) == "old"
	assert tracker.missing_count == 0


def test_bitrate_controller_uses_hysteresis_and_waits_before_rising():
	controller = audio_protocol.BitrateController(
		lower_observations=2,
		raise_observations=3,
		min_change_interval=5,
	)

	assert controller.observe(3, now=0) is None
	assert controller.observe(3, now=1) == 64000
	assert controller.observe(0, now=2) is None
	assert controller.observe(0, now=3) is None
	assert controller.observe(0, now=4) is None
	assert controller.observe(0, now=6) == 128000

	controller = audio_protocol.BitrateController(lower_observations=2)
	assert controller.observe(8, dropped=True, now=0) is None
	assert controller.observe(8, dropped=True, now=1) == 48000
