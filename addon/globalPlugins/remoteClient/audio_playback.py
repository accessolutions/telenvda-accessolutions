"""Playing the sound of the other computer, on the one that listens.

NVDA already knows how to talk to a sound card, and does it in a way that respects
what the user configured, so nothing new is opened here: its own player is asked for
a second stream, alongside the speech, and the sound arriving from the other machine
is poured into it.

The one thing that has to be watched is which thread does the pouring. Feeding a
sound card blocks until the card has room, so doing it on the thread that reads the
network would hold up every other message of the session, speech included. A thread
of its own is used instead, and the network thread only drops sound into a queue.

That queue is also what absorbs the unevenness of the network. Sound arrives in
bursts and has to leave at a steady rate, so a little is held back before playing
starts; the delay this adds is of no consequence when one is listening to a computer
rather than talking to a person.
"""

import queue
import threading

import nvwave
# NVDA only adds its handlers to its own logger, so a logger of this module's own
# would write nothing at all below the warning level.
from logHandler import log as logger

from .audio_capture import OUTPUT_CHANNELS, OUTPUT_RATE



class Player:
	"""A sound card stream fed from a queue by a thread of its own."""

	#: Beyond this many pending blocks the listener is hopelessly behind and the
	#: oldest sound is thrown away. Sound that plays late is worse than sound lost.
	MAX_PENDING = 50

	#: Blocks held back before playing starts, so that a first hiccup of the network
	#: does not turn into a click.
	PREBUFFER = 3

	def __init__(self, volume=1.0):
		self.volume = volume
		self._queue = queue.Queue()
		self._flowing = threading.Event()
		self._thread = None
		self._running = False

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
		while self._queue.qsize() >= self.MAX_PENDING:
			try:
				self._queue.get_nowait()
			except queue.Empty:
				break
		self._queue.put(block)
		if self._queue.qsize() >= self.PREBUFFER:
			self._flowing.set()

	def stop(self):
		if not self._running:
			return
		self._running = False
		self._flowing.set()
		self._queue.put(None)
		thread, self._thread = self._thread, None
		if thread is not None and thread is not threading.current_thread():
			thread.join(timeout=2.0)

	def _run(self):
		player = None
		try:
			player = _open_player()
		except Exception:
			logger.exception("Unable to open the sound card to play the remote sound")
			self._running = False
			return
		try:
			# Nothing is played until enough has arrived to ride out the first gap.
			self._flowing.wait(timeout=2.0)
			while True:
				block = self._queue.get()
				if block is None or not self._running:
					break
				player.feed(block)
		except Exception:
			logger.exception("Unable to play the remote sound")
		finally:
			self._running = False
			for method in ("stop", "close"):
				try:
					getattr(player, method)()
				except Exception:
					logger.debug("Unable to %s the remote audio player", method, exc_info=True)


def _open_player():
	"""Open a stream on the sound card NVDA itself speaks through.

	The signature of the player has moved between NVDA versions, and the setting
	naming the output device moved from the speech section to one of its own. Both
	are tried and neither is required, since the default is the right device on
	almost every machine.
	"""
	arguments = dict(channels=OUTPUT_CHANNELS, samplesPerSec=OUTPUT_RATE, bitsPerSample=16)
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
