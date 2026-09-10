import importlib.util
import logging
from pathlib import Path
import struct
import sys
import types

import pytest


pytestmark = pytest.mark.skipif(
	sys.platform != "win32",
	reason="audio_capture uses the Windows audio API",
)


MODULE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient/audio_capture.py"


def _load_audio_capture(monkeypatch):
	monkeypatch.setitem(
		sys.modules,
		"logHandler",
		types.SimpleNamespace(log=logging.getLogger("test_audio_capture")),
	)
	spec = importlib.util.spec_from_file_location("test_audio_capture", MODULE_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _stereo_block(audio_capture, left, right):
	values = []
	for _ in range(audio_capture.BLOCK_FRAMES):
		values.extend((left, right))
	return struct.pack("<" + "h" * len(values), *values)


class FakeCapture:
	def __init__(self, on_block):
		self.on_block = on_block

	def start(self):
		return None

	def stop(self):
		return None


def _mixer_with_sources(audio_capture, source_count):
	created = []
	mixer = audio_capture.Mixer(
		lambda block: None,
		output_rate=audio_capture.SAMPLE_RATE,
		output_channels=audio_capture.CHANNELS,
	)
	for index in range(source_count):
		mixer.add(str(index), lambda on_block: created.append(FakeCapture(on_block)) or created[-1])
	return mixer, created


def test_stereo_source_preserves_both_channels_without_downmix(monkeypatch):
	audio_capture = _load_audio_capture(monkeypatch)
	monkeypatch.setattr(audio_capture, "downmix", lambda block: pytest.fail("downmix must not run"))
	mixer, captures = _mixer_with_sources(audio_capture, 1)
	block = _stereo_block(audio_capture, 12000, -6000)

	captures[0].on_block(block)

	assert mixer._mix() == block


def test_stereo_sources_are_added_per_channel_and_clipped(monkeypatch):
	audio_capture = _load_audio_capture(monkeypatch)
	mixer, captures = _mixer_with_sources(audio_capture, 2)

	captures[0].on_block(_stereo_block(audio_capture, 30000, -30000))
	captures[1].on_block(_stereo_block(audio_capture, 10000, -10000))

	mixed = mixer._mix()
	samples = struct.unpack("<" + "h" * (len(mixed) // 2), mixed)
	assert samples[::2] == (32767,) * audio_capture.BLOCK_FRAMES
	assert samples[1::2] == (-32768,) * audio_capture.BLOCK_FRAMES


def test_stereo_mixer_uses_opus_frame_size(monkeypatch):
	audio_capture = _load_audio_capture(monkeypatch)
	mixer, _ = _mixer_with_sources(audio_capture, 0)

	assert mixer.output_block_bytes == 3840
	assert len(mixer._mix()) == 3840
