import importlib.util
from pathlib import Path
import sys
import types


MODULE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient/audio_playback.py"


class FakeWavePlayer:
	instances = []

	def __init__(self, **kwargs):
		self.arguments = kwargs
		self.blocks = []
		self.stopped = False
		self.closed = False
		self.instances.append(self)

	def feed(self, block):
		self.blocks.append(block)

	def stop(self):
		self.stopped = True

	def close(self):
		self.closed = True


class FakeEncoder:
	instances = []

	def __init__(self, **kwargs):
		self.arguments = kwargs
		self.closed = False
		self.instances.append(self)

	def encode(self, pcm):
		self.input = pcm
		return b"encoded"

	def close(self):
		self.closed = True


class FakeDecoder:
	instances = []

	def __init__(self, **kwargs):
		self.arguments = kwargs
		self.gain = None
		self.closed = False
		self.instances.append(self)

	def set_gain_percent(self, gain):
		self.gain = gain

	def decode(self, packet):
		self.input = packet
		return b"decoded"

	def close(self):
		self.closed = True


def _load_audio_playback(monkeypatch):
	package = types.ModuleType("remoteClient")
	package.__path__ = [str(MODULE_PATH.parent)]
	monkeypatch.setitem(sys.modules, "remoteClient", package)
	monkeypatch.setitem(
		sys.modules,
		"remoteClient.audio_capture",
		types.SimpleNamespace(OUTPUT_CHANNELS=1, OUTPUT_RATE=16000),
	)
	monkeypatch.setitem(
		sys.modules,
		"remoteClient.audio_opus",
		types.SimpleNamespace(
			CHANNELS=2,
			SAMPLE_RATE=48000,
			OpusEncoder=FakeEncoder,
			OpusDecoder=FakeDecoder,
		),
	)
	monkeypatch.setitem(sys.modules, "nvwave", types.SimpleNamespace(WavePlayer=FakeWavePlayer))
	monkeypatch.setitem(
		sys.modules,
		"logHandler",
		types.SimpleNamespace(log=types.SimpleNamespace(exception=lambda *args: None, debug=lambda *args, **kwargs: None)),
	)
	spec = importlib.util.spec_from_file_location("remoteClient.audio_playback", MODULE_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def test_local_opus_player_round_trip_uses_stereo_wave_player(monkeypatch):
	FakeWavePlayer.instances.clear()
	FakeEncoder.instances.clear()
	FakeDecoder.instances.clear()
	audio_playback = _load_audio_playback(monkeypatch)

	player = audio_playback.LocalOpusPlayer(volume=0.8)
	player.feed(b"pcm stereo frame")

	encoder = FakeEncoder.instances[0]
	decoder = FakeDecoder.instances[0]
	wave_player = FakeWavePlayer.instances[0]
	assert wave_player.arguments["channels"] == 2
	assert wave_player.arguments["samplesPerSec"] == 48000
	assert wave_player.arguments["bitsPerSample"] == 16
	assert encoder.input == b"pcm stereo frame"
	assert decoder.input == b"encoded"
	assert decoder.gain == 80
	assert wave_player.blocks == [b"decoded"]

	player.stop()
	assert encoder.closed is True
	assert decoder.closed is True
	assert wave_player.stopped is True
	assert wave_player.closed is True


def test_timed_audio_buffer_keeps_a_bounded_duration_and_drops_oldest(monkeypatch):
	audio_playback = _load_audio_playback(monkeypatch)
	buffer = audio_playback.TimedAudioBuffer(frame_ms=20, prebuffer_ms=100, target_ms=200, max_ms=500)

	for value in range(30):
		buffer.put(bytes([value]))

	assert buffer.buffered_ms == 500
	assert buffer.dropped_blocks == 5
	assert buffer.wait_for_prebuffer(timeout=0) is True
	assert buffer.get() == bytes([5])


def test_timed_audio_buffer_rearms_after_underrun_and_reset(monkeypatch):
	audio_playback = _load_audio_playback(monkeypatch)
	buffer = audio_playback.TimedAudioBuffer(frame_ms=20, prebuffer_ms=40, target_ms=200, max_ms=500)

	buffer.put(b"a")
	buffer.put(b"b")
	assert buffer.wait_for_prebuffer(timeout=0) is True
	assert buffer.get() == b"a"
	assert buffer.get() == b"b"
	assert buffer.underruns == 1
	assert buffer.wait_for_prebuffer(timeout=0) is False

	buffer.put(b"c")
	buffer.reset()
	assert buffer.buffered_ms == 0
	assert buffer.wait_for_prebuffer(timeout=0) is False
