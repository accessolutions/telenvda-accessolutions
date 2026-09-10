"""Playing the sound of the other computer, on the one that listens.

NVDA already knows how to talk to a sound card, and does it in a way that respects
what the user configured, so nothing new is opened here: its own player is asked for
a second stream, alongside the speech, and the sound arriving from the other machine
is poured into it.

The one thing that has to be watched is which thread does the pouring. Feeding a
sound card blocks until the card has room, so doing it on the thread that reads the
network would hold up every other message of the session, speech included. A thread
of its own is used instead, and the network thread only drops sound into a bounded
time buffer.
"""

from collections import deque
import threading

import nvwave
# NVDA only adds its handlers to its own logger, so a logger of this module's own
# would write nothing at all below the warning level.
from logHandler import log as logger

from .audio_capture import OUTPUT_CHANNELS, OUTPUT_RATE
from . import audio_opus


class TimedAudioBuffer:
	"""Bounded PCM buffer whose limits are expressed in milliseconds."""

	def __init__(self, frame_ms=20, prebuffer_ms=100, target_ms=200, max_ms=500):
		if frame_ms <= 0 or prebuffer_ms <= 0 or target_ms <= 0 or max_ms < target_ms:
			raise ValueError("invalid audio buffer duration")
		self.frame_ms = frame_ms
		self.prebuffer_frames = (prebuffer_ms + frame_ms - 1) // frame_ms
		self.target_ms = target_ms
		self.max_frames = max_ms // frame_ms
		if self.max_frames < self.prebuffer_frames:
			raise ValueError("the audio buffer ceiling must contain its prebuffer")
		self._blocks = deque()
		self._condition = threading.Condition()
		self._closed = False
		self._started = False
		self._dropped_blocks = 0
		self._underruns = 0

	@property
	def buffered_ms(self):
		with self._condition:
			return len(self._blocks) * self.frame_ms

	@property
	def dropped_blocks(self):
		with self._condition:
			return self._dropped_blocks

	@property
	def underruns(self):
		with self._condition:
			return self._underruns

	def put(self, block):
		with self._condition:
			if self._closed:
				return False
			while len(self._blocks) >= self.max_frames:
				self._blocks.popleft()
				self._dropped_blocks += 1
			self._blocks.append(bytes(block))
			self._condition.notify_all()
			return True

	def wait_for_prebuffer(self, timeout=None):
		with self._condition:
			if self._started:
				return True
			ready = self._condition.wait_for(
				lambda: self._closed or len(self._blocks) >= self.prebuffer_frames,
				timeout,
			)
			if self._closed or not ready:
				return False
			self._started = True
			return True

	def get(self, timeout=None):
		with self._condition:
			ready = self._condition.wait_for(lambda: self._closed or self._blocks, timeout)
			if not ready or not self._blocks:
				return None
			block = self._blocks.popleft()
			if self._started and not self._blocks:
				self._started = False
				self._underruns += 1
			return block

	def reset(self):
		with self._condition:
			self._blocks.clear()
			self._started = False
			self._condition.notify_all()

	def close(self):
		with self._condition:
			self._closed = True
			self._blocks.clear()
			self._condition.notify_all()


class Player:
	"""A sound card stream fed from a bounded time buffer by its own thread."""

	def __init__(
		self,
		volume=1.0,
		channels=OUTPUT_CHANNELS,
		samples_per_sec=OUTPUT_RATE,
		frame_ms=200,
		prebuffer_ms=100,
		target_ms=200,
		max_buffer_ms=500,
	):
		self.volume = volume
		self.channels = channels
		self.samples_per_sec = samples_per_sec
		self.target_ms = target_ms
		self.max_buffer_ms = max_buffer_ms
		self._buffer = TimedAudioBuffer(frame_ms, prebuffer_ms, target_ms, max_buffer_ms)
		self._thread = None
		self._running = False
		self._reported_drops = 0
		self._reported_underruns = 0

	def start(self):
		if self._running:
			return
		self._running = True
		self._thread = threading.Thread(target=self._run, name="remote_audio_playback", daemon=True)
		self._thread.start()

	def feed(self, block):
		"""Hand over sound to be played. Called from the thread reading the network."""
		if not self._running:
			return
		self._buffer.put(block)
		dropped = self._buffer.dropped_blocks
		if dropped > self._reported_drops:
			self._reported_drops = dropped
			logger.warning(
				"Remote audio buffer dropped old blocks: depth=%d ms dropped=%d",
				self._buffer.buffered_ms,
				dropped,
			)

	@property
	def buffered_ms(self):
		return self._buffer.buffered_ms

	@property
	def dropped_blocks(self):
		return self._buffer.dropped_blocks

	@property
	def underruns(self):
		return self._buffer.underruns

	def reset(self):
		self._buffer.reset()
		logger.info("Remote audio playback buffer reset")

	def stop(self):
		if not self._running:
			return
		self._running = False
		self._buffer.close()
		thread, self._thread = self._thread, None
		if thread is not None and thread is not threading.current_thread():
			thread.join(timeout=2.0)

	def _run(self):
		player = None
		try:
			player = _open_player(channels=self.channels, samples_per_sec=self.samples_per_sec)
		except Exception:
			logger.exception("Unable to open the sound card to play the remote sound")
			self._running = False
			return
		try:
			while self._running:
				if not self._buffer.wait_for_prebuffer(timeout=0.5):
					continue
				block = self._buffer.get(timeout=0.5)
				if block is None:
					continue
				player.feed(block)
				underruns = self._buffer.underruns
				if underruns > self._reported_underruns:
					self._reported_underruns = underruns
					logger.warning(
						"Remote audio playback underflow: depth=%d ms underruns=%d",
						self._buffer.buffered_ms,
						underruns,
					)
		except Exception:
			logger.exception("Unable to play the remote sound")
		finally:
			self._running = False
			for method in ("stop", "close"):
				try:
					getattr(player, method)()
				except Exception:
					logger.debug("Unable to %s the remote audio player", method, exc_info=True)


class OpusPlayer:
	"""Decode remote Opus frames and feed the bounded PCM playback buffer."""

	def __init__(self, volume=1.0):
		if not 0.0 <= volume <= 1.0:
			raise ValueError("volume must be between 0 and 1")
		decoder = audio_opus.OpusDecoder()
		player = Player(
			channels=audio_opus.CHANNELS,
			samples_per_sec=audio_opus.SAMPLE_RATE,
			frame_ms=audio_opus.FRAME_MS,
		)
		try:
			decoder.set_gain_percent(round(volume * 100))
			player.start()
		except Exception:
			decoder.close()
			player.stop()
			raise
		self._decoder = decoder
		self._player = player

	@property
	def buffered_ms(self):
		return self._player.buffered_ms

	@property
	def dropped_blocks(self):
		return self._player.dropped_blocks

	@property
	def underruns(self):
		return self._player.underruns

	@property
	def target_ms(self):
		return self._player.target_ms

	@property
	def max_buffer_ms(self):
		return self._player.max_buffer_ms

	def feed_packet(self, packet):
		self._player.feed(self._decoder.decode(packet))

	def feed_lost(self):
		self._player.feed(self._decoder.decode_lost())

	def reset_stream(self):
		self._decoder.reset()
		self._player.reset()

	def stop(self):
		player, self._player = self._player, None
		decoder, self._decoder = self._decoder, None
		if player is not None:
			player.stop()
		if decoder is not None:
			decoder.close()


class LocalOpusPlayer:
	"""Encode and decode one local PCM path before feeding NVDA's sound player."""

	def __init__(self, volume=1.0):
		if not 0.0 <= volume <= 1.0:
			raise ValueError("volume must be between 0 and 1")
		encoder = decoder = player = None
		try:
			encoder = audio_opus.OpusEncoder()
			decoder = audio_opus.OpusDecoder()
			decoder.set_gain_percent(round(volume * 100))
			player = _open_player(
				channels=audio_opus.CHANNELS,
				samples_per_sec=audio_opus.SAMPLE_RATE,
			)
		except Exception:
			if decoder is not None:
				decoder.close()
			if encoder is not None:
				encoder.close()
			if player is not None:
				for method in ("stop", "close"):
					try:
						getattr(player, method)()
					except Exception:
						logger.debug("Unable to %s the local Opus player", method, exc_info=True)
			raise
		self._encoder = encoder
		self._decoder = decoder
		self._player = player

	def feed(self, pcm):
		if self._player is None:
			raise RuntimeError("local Opus player is stopped")
		packet = self._encoder.encode(pcm)
		self._player.feed(self._decoder.decode(packet))

	def stop(self):
		player, self._player = self._player, None
		decoder, self._decoder = self._decoder, None
		encoder, self._encoder = self._encoder, None
		if decoder is not None:
			decoder.close()
		if encoder is not None:
			encoder.close()
		if player is not None:
			for method in ("stop", "close"):
				try:
					getattr(player, method)()
				except Exception:
					logger.debug("Unable to %s the local Opus player", method, exc_info=True)


def _open_player(channels=OUTPUT_CHANNELS, samples_per_sec=OUTPUT_RATE):
	"""Open a stream on the sound card NVDA itself speaks through.

	The signature of the player has moved between NVDA versions, and the setting
	naming the output device moved from the speech section to one of its own. Both
	are tried and neither is required, since the default is the right device on
	almost every machine.
	"""
	arguments = dict(channels=channels, samplesPerSec=samples_per_sec, bitsPerSample=16)
	device = _output_device()
	attempts = []
	if device is not None:
		attempts.append(dict(arguments, outputDevice=device, wantDucking=False))
		attempts.append(dict(arguments, outputDevice=device))
	attempts.append(dict(arguments, wantDucking=False))
	attempts.append(arguments)
	last = None
	for attempt in attempts:
		try:
			return nvwave.WavePlayer(**attempt)
		except Exception as error:
			last = error
	raise last


def _output_device():
	try:
		import config

		for section, key in (("audio", "outputDevice"), ("speech", "outputDevice")):
			try:
				value = config.conf[section][key]
			except Exception:
				continue
			if value:
				return value
	except Exception:
		logger.debug("Unable to read the configured output device", exc_info=True)
	return None
