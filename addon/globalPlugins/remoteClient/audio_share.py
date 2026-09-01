"""Remote audio: the sound of the assisted computer, heard on the assisting one.

The picture is not always what is missing. A sound that plays at the wrong moment,
a video whose commentary matters, an alert nobody can describe: the person helping
often needs to hear the machine rather than see it. This carries that sound, on its
own, without any screen sharing session having to be started first.

What travels is not everything the sound card plays. Windows can tell one program's
sound from another's, and that is used here: the assisting user names the
applications they do not want to hear, and each of the others is captured
separately and mixed. The screen reader running on the assisted computer is never
among them, so its voice is not heard twice.

Unlike the picture, the sound takes the same road as everything else in the session:
it is cut into short pieces, folded down to what a telephone carries, and sent as
ordinary messages of the protocol. It is therefore encrypted exactly like the rest
of the session when end to end encryption is on, it needs no relay of its own and no
program outside NVDA, and it crosses the firewalls that already let the session
through. It does pass through the relay, which the picture does not, and it arrives
half a second late; neither matters when one is listening to a computer rather than
speaking to a person.

The cost on the line is about a hundred and seventy kilobits a second, roughly half
of what screen sharing at its lowest quality asks for.
"""

import base64
import threading

import wx

import addonHandler
import gui
import ui
# NVDA only adds its handlers to its own logger, so a logger of this module's own
# would write nothing at all below the warning level.
from logHandler import log as logger

from . import capabilities, configuration
from .screen_share import ROLE_PUBLISHER, ROLE_VIEWER, STATE_ACTIVE, STATE_IDLE, STATE_REQUESTING
from .transport import TransportEvents


#: Capturing sound, listing what is playing and pouring it back into a sound card
#: all reach into Windows through raw pointers and into NVDA's own player. A machine
#: where any of that is refused must still be able to be assisted, so a failure here
#: turns the feature off instead of taking the whole add-on down with it.
try:
	from . import audio_capture, audio_playback, audio_sources
except Exception:
	audio_capture = audio_playback = audio_sources = None
	logger.exception("Remote audio cannot be used on this computer")

try:
	addonHandler.initTranslation()
except addonHandler.AddonError:
	logger.warning("Unable to initialise translations. This may be because the addon is running from NVDA scratchpad.")

#: The messages of the feature. They are ordinary messages of the protocol, so the
#: relay passes them to the other member of the channel without knowing what they
#: are, and the session encrypts them along with everything else.
MSG_REQUEST = "remote_audio_request"
MSG_RESPONSE = "remote_audio_response"
MSG_STOP = "remote_audio_stop"
MSG_DATA = "remote_audio_data"
MSG_SOURCES = "remote_audio_sources"
MSG_EXCLUDE = "remote_audio_exclude"

#: Blocks of sound gathered before one message is sent. Twenty milliseconds at a
#: time would mean fifty messages a second, each carrying less than it costs to
#: describe; two hundred brings that down to five, which the session hardly feels.
BLOCKS_PER_MESSAGE = 10

#: Longest piece of sound accepted in one message, before it is decoded. Anything
#: larger than a second of sound is not something this feature ever sends.
_MAX_SOUND_PAYLOAD = 32 * 1024

#: Messages already waiting to leave beyond which the sound is dropped rather than
#: queued. Every other message of the session is short and occasional, so nothing
#: else would ever fill that queue; a stream would fill it without end on a line
#: too slow for it, and the sound would arrive later and later while the memory
#: kept growing. Dropping is the right answer: sound that plays late is worse than
#: sound that is never played.
_MAX_QUEUED_MESSAGES = 20

#: How often the list of applications playing something is looked at again.
#: A program that starts playing has to be picked up quickly, but enumerating the
#: audio sessions of the system is not free either.
_REFRESH_INTERVAL = 2.0

#: Longest list of application names accepted from a peer.
_MAX_EXCLUSIONS = 200


def is_enabled():
	"""Whether the user left remote audio turned on."""
	try:
		return bool(configuration.get_config()["remote_audio"]["enabled"])
	except Exception:
		logger.debug("Unable to read the remote audio configuration", exc_info=True)
		return False


def is_available():
	"""Whether this installation can take part in a remote audio session.

	Nothing is needed beyond NVDA itself: no browser, no encoder, no port to open.
	The setting is therefore almost the only thing there is to look at, the other being
	whether the capture could be loaded at all on this computer.
	"""
	return audio_capture is not None and is_enabled()


def excluded_applications():
	"""Return the executables the user of this computer does not want to hear.

	Only the exceptions are kept, never the whole list of what was allowed: a
	machine that assists many others would otherwise slowly build an inventory of
	everything those others ever ran.
	"""
	try:
		names = configuration.get_config()["remote_audio"]["excluded_applications"]
	except Exception:
		logger.debug("Unable to read the excluded applications", exc_info=True)
		return []
	return sorted({str(name).strip().lower() for name in names if str(name).strip()})


def set_excluded_applications(names):
	config = configuration.get_config()
	config["remote_audio"]["excluded_applications"] = sorted(
		{str(name).strip().lower() for name in names if str(name).strip()}
	)
	config.write()


def playback_volume():
	"""Return the share of the volume the remote sound is played at, from 0 to 1."""
	try:
		value = int(configuration.get_config()["remote_audio"]["volume"])
	except Exception:
		value = 80
	return max(0, min(100, value)) / 100.0


class AudioShareManager:
	"""Drive a remote audio session and carry both its control and its sound."""

	def __init__(self, transport, negotiator, role):
		self.transport = transport
		self.negotiator = negotiator
		self.role = role
		self.state = STATE_IDLE
		self.peer_id = None
		#: The whole sound card is captured when Windows is too old to separate the
		#: applications. The user is told, because their own screen reader is then
		#: part of what the other computer hears.
		self.whole_device = False
		self._mixer = None
		self._player = None
		self._decode = None
		self._excluded = set()
		self._refresh = None
		self._stop_refresh = threading.Event()
		#: Blocks waiting to be gathered into one message, on the sending side.
		self._pending = []
		#: Applications heard during this session, by executable name.
		self._sounding = {}
		#: Applications the other computer says it is playing, on the listening side.
		self._remote_sounding = []
		callbacks = transport.callback_manager
		callbacks.register_callback("msg_" + MSG_REQUEST, self.handle_request)
		callbacks.register_callback("msg_" + MSG_RESPONSE, self.handle_response)
		callbacks.register_callback("msg_" + MSG_STOP, self.handle_stop)
		callbacks.register_callback("msg_" + MSG_DATA, self.handle_data)
		callbacks.register_callback("msg_" + MSG_SOURCES, self.handle_sources)
		callbacks.register_callback("msg_" + MSG_EXCLUDE, self.handle_exclude)
		callbacks.register_callback(TransportEvents.DISCONNECTED, self.terminate)

	# Session control.

	@property
	def active(self):
		return self.state != STATE_IDLE

	def sounding_applications(self):
		"""Return the executables heard so far in this session.

		On the computer that listens this is what the other one said it was playing,
		which is what the dialog offering to silence some of them needs.
		"""
		if self.role == ROLE_VIEWER:
			return list(self._remote_sounding)
		return sorted(set(self._sounding.values()))

	def unwanted_applications(self):
		"""Return the applications this session is currently leaving out."""
		return sorted(self._excluded)

	def set_unwanted_applications(self, names):
		"""Change, in the middle of a session, which applications are left out.

		The list belongs to the computer that listens, so that one only sends it and
		the assisted computer is the one that acts on it. Waiting for the next look at
		what is playing would leave a program audible for another couple of seconds
		after it was silenced, so the captures already running are dropped at once.
		"""
		names = {str(name).strip().lower() for name in names if str(name).strip()}
		if not self.active:
			return
		if self.role == ROLE_VIEWER:
			self._send(MSG_EXCLUDE, excluded=sorted(names)[:_MAX_EXCLUSIONS])
			return
		self._excluded = names
		self._drop_unwanted()

	def toggle(self):
		"""Start the session when there is none, stop the current one otherwise.

		Returns a message to report to the user.
		"""
		if self.active:
			self.stop()
			# Translators: message spoken when remote audio is turned off
			return _("Remote audio stopped")
		return self.start()

	def start(self):
		"""Ask the assisted computer for its sound. Returns a message to report."""
		if self.role != ROLE_VIEWER:
			# Translators: message spoken when remote audio is requested from the wrong computer
			return _("Remote audio can only be started from the controlling computer")
		if not is_available():
			# Translators: message spoken when remote audio cannot run on this computer
			return _("Remote audio is not available on this computer")
		peers = self.negotiator.peers_supporting(capabilities.FEATURE_REMOTE_AUDIO)
		if not peers:
			# Translators: message spoken when the other computer cannot share its sound
			return _("The other computer does not support remote audio")
		self.peer_id = peers[0]
		self.state = STATE_REQUESTING
		# Which applications are wanted is decided here, on the computer that listens,
		# and travels with the request. The assisted computer keeps nothing of it.
		self._send(MSG_REQUEST, excluded=excluded_applications())
		# Translators: message spoken when remote audio has been requested
		return _("Remote audio requested")

	def stop(self, notify_peer=True):
		"""End the current session, telling the peer about it unless it asked for it."""
		if not self.active:
			return
		if notify_peer and self.peer_id is not None:
			self._send(MSG_STOP)
		self.state = STATE_IDLE
		self.peer_id = None
		self.whole_device = False
		self._stop_capture()
		self._stop_playback()

	def terminate(self):
		self.stop(notify_peer=False)

	# The capture, on the computer being listened to.

	def _start_capture(self, excluded):
		self._excluded = set(excluded)
		self._sounding = {}
		self._pending = []
		self._mixer = audio_capture.Mixer(self._on_mixed_block)
		self.whole_device = not audio_capture.is_process_capture_available()
		if self.whole_device:
			# Before Windows 10 version 2004 there is no way to capture one program on
			# its own. Everything the sound card plays is sent instead, which includes
			# the voice of the screen reader running here.
			self._mixer.add("system", audio_capture.SystemCapture)
		else:
			self._stop_refresh.clear()
			self._refresh = threading.Thread(
				target=self._follow_sources, name="audio_share_sources", daemon=True
			)
			self._refresh.start()
		self._mixer.start()

	def _stop_capture(self):
		self._stop_refresh.set()
		self._refresh = None
		self._pending = []
		mixer, self._mixer = self._mixer, None
		if mixer is not None:
			mixer.stop()

	def _on_mixed_block(self, block):
		"""Gather the mixed sound and send it once there is enough to be worth a message.

		This runs on the thread that paces the mixing, so what is done here has to
		stay short: folding sixteen bit samples into eight is a table lookup, and the
		message goes out on the transport's own sending queue.

		A stretch where nothing at all is playing is not sent. The mixer runs on a
		clock and therefore always has something to hand over, but sending silence
		would cost the line as much as sending music, and the other end has nothing
		to do with it.
		"""
		self._pending.append(block)
		if len(self._pending) < BLOCKS_PER_MESSAGE:
			return
		blocks, self._pending = self._pending, []
		if self.state == STATE_IDLE:
			return
		if all(block == audio_capture.SILENCE for block in blocks):
			return
		if self._sending_is_behind():
			return
		try:
			sound = base64.b64encode(audio_capture.compress(b"".join(blocks))).decode("ascii")
		except Exception:
			logger.exception("Unable to prepare the sound of this computer")
			return
		self._send(MSG_DATA, sound=sound)

	def _sending_is_behind(self):
		try:
			return self.transport.queue.qsize() > _MAX_QUEUED_MESSAGES
		except Exception:
			return False

	def _follow_sources(self):
		"""Keep one capture running per application that is allowed and playing.

		Windows only lets a capture name a single process tree, so nothing can be said
		as "everything except these two". Each wanted application is therefore captured
		on its own and the results are summed, which is also what makes the screen
		reader of the assisted computer impossible to let through by accident.
		"""
		while not self._stop_refresh.is_set():
			try:
				self._refresh_sources()
			except Exception:
				logger.exception("Unable to look at the applications playing sound")
			if self._stop_refresh.wait(_REFRESH_INTERVAL):
				return

	def _refresh_sources(self):
		mixer = self._mixer
		if mixer is None:
			return
		before = sorted(set(self._sounding.values()))
		own = audio_capture.own_process_tree_root()
		wanted = {}
		found = []
		for session in audio_sources.list_sessions():
			found.append(session["name"])
			if session["pid"] == own:
				continue
			if session["name"] in self._excluded:
				continue
			wanted[session["pid"]] = session["name"]
		running = mixer.keys
		for pid in running - set(wanted):
			mixer.remove(pid)
			self._sounding.pop(pid, None)
		for pid, name in wanted.items():
			if pid in running:
				continue
			mixer.add(pid, lambda on_block, pid=pid: audio_capture.ProcessCapture(pid, on_block))
			self._sounding[pid] = name
		logger.debug(
			"Remote audio, applications holding a sound session: %s ; captured: %s",
			", ".join(sorted(set(found))) or "none",
			", ".join(sorted(set(wanted.values()))) or "none",
		)
		self._announce_sources(before)

	def _drop_unwanted(self):
		"""Stop capturing the applications which are no longer wanted."""
		mixer = self._mixer
		if mixer is None:
			return
		before = sorted(set(self._sounding.values()))
		for pid, name in list(self._sounding.items()):
			if name in self._excluded:
				mixer.remove(pid)
				self._sounding.pop(pid, None)
		self._announce_sources(before)

	def _announce_sources(self, before):
		"""Tell the listening computer what it is hearing, when that changed.

		Without this the other end would have nothing to offer but a list of names typed
		by hand, and no way of knowing that a program it never heard of started playing.
		"""
		names = sorted(set(self._sounding.values()))
		if names == before:
			return
		logger.info("Remote audio, telling the other computer it is hearing: %s", ", ".join(names) or "nothing")
		try:
			self._send(MSG_SOURCES, applications=names)
		except Exception:
			logger.exception("Unable to send the list of applications being heard")

	# The playing, on the computer that listens.

	def _start_playback(self):
		volume = playback_volume()
		# The volume is folded into the table the sound is decoded with, so turning it
		# down costs nothing at all once the session has started.
		self._decode = audio_capture.decode_table(volume)
		self._player = audio_playback.Player()
		self._player.start()

	def _stop_playback(self):
		player, self._player = self._player, None
		self._decode = None
		if player is not None:
			player.stop()

	# Messages received from the peer.

	def handle_request(self, origin=None, excluded=None, **kwargs):
		"""The assisting computer asks to hear this one."""
		if not self._accept_from(origin):
			return
		if self.role != ROLE_PUBLISHER or not is_available():
			self._refuse(origin, "unavailable")
			return
		if self.active:
			self._refuse(origin, "busy")
			return
		names = []
		if isinstance(excluded, list):
			for name in excluded[:_MAX_EXCLUSIONS]:
				if isinstance(name, str) and name.strip():
					names.append(name.strip().lower())
		wx.CallAfter(self._ask_permission, origin, names)

	def _ask_permission(self, origin, excluded):
		if audio_capture.is_process_capture_available():
			# Translators: question asked before the sound of this computer is shared
			message = _("Do you want to share the sound of this computer? The controlling computer will hear the applications playing here.")
		else:
			# This Windows cannot capture one program on its own, so consent has to be
			# asked for what will really happen rather than for what usually happens.
			# Translators: question asked before the sound of this computer is shared, when Windows cannot separate the applications
			message = _("Do you want to share the sound of this computer? This version of Windows cannot separate the applications, so everything played here will be heard, including the speech of this screen reader.")
		answer = gui.messageBox(
			parent=gui.mainFrame,
			# Translators: title of the remote audio request dialog
			caption=_("Remote audio request"),
			message=message,
			style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
		)
		if answer == wx.YES:
			self._accept_request(origin, excluded)
		else:
			self._refuse(origin, "declined")

	def _accept_request(self, origin, excluded):
		if self.active:
			self._refuse(origin, "busy")
			return
		self.peer_id = origin
		self.state = STATE_ACTIVE
		try:
			self._start_capture(excluded)
		except Exception:
			logger.exception("Unable to start capturing the sound of this computer")
			self.stop(notify_peer=False)
			self._refuse(origin, "unavailable")
			return
		self._send(MSG_RESPONSE, accepted=True, whole_device=self.whole_device)
		if self.whole_device:
			# Translators: message spoken when the sound of the whole computer is shared
			ui.message(_("Sharing all the sound of this computer, including this speech"))
		else:
			# Translators: message spoken on the assisted computer when it starts sharing its sound
			ui.message(_("Sharing the sound of this computer"))

	def _refuse(self, origin, reason):
		self._send(MSG_RESPONSE, target=origin, accepted=False, reason=reason)

	def handle_response(self, origin=None, accepted=False, whole_device=False, reason="", **kwargs):
		if not self._accept_from(origin) or origin != self.peer_id:
			return
		if self.state != STATE_REQUESTING:
			return
		if not accepted:
			self.state = STATE_IDLE
			self.peer_id = None
			wx.CallAfter(ui.message, _refusal_message(reason))
			return
		try:
			self._start_playback()
		except Exception:
			logger.exception("Unable to open the sound card for the remote sound")
			self.state = STATE_IDLE
			self.peer_id = None
			# Translators: message spoken when the remote sound cannot be played here
			wx.CallAfter(ui.message, _("Unable to play the remote sound on this computer"))
			return
		self.state = STATE_ACTIVE
		self.whole_device = bool(whole_device)
		# Translators: message spoken when the remote sound starts being heard
		wx.CallAfter(ui.message, _("Remote audio started"))
		if self.whole_device:
			# Translators: message spoken when the assisted computer can only share all of its sound
			wx.CallAfter(ui.message, _("This computer can only share all of its sound, including the speech of its screen reader"))

	def handle_stop(self, origin=None, **kwargs):
		if not self._accept_from(origin) or origin != self.peer_id:
			return
		self.stop(notify_peer=False)
		# Translators: message spoken when the other computer ended remote audio
		wx.CallAfter(ui.message, _("Remote audio ended"))

	def handle_data(self, origin=None, sound=None, **kwargs):
		"""A piece of the other computer's sound has arrived.

		This runs on the thread reading the network, so it must never wait: the sound
		is decoded, which is a table lookup, and handed to the player's queue.
		"""
		if self.state != STATE_ACTIVE or origin != self.peer_id:
			return
		player = self._player
		if player is None or not isinstance(sound, str) or len(sound) > _MAX_SOUND_PAYLOAD:
			return
		try:
			player.feed(audio_capture.expand(base64.b64decode(sound, validate=True), self._decode))
		except Exception:
			logger.exception("Unable to decode the sound of the other computer")

	def handle_sources(self, applications=None, **kwargs):
		if self.role != ROLE_VIEWER or not isinstance(applications, list):
			return
		self._remote_sounding = sorted({
			name.strip().lower() for name in applications[:_MAX_EXCLUSIONS]
			if isinstance(name, str) and name.strip()
		})
		logger.info(
			"Remote audio, the other computer says it is playing: %s",
			", ".join(self._remote_sounding) or "nothing",
		)

	def handle_exclude(self, origin=None, excluded=None, **kwargs):
		"""The listening computer changed which of the applications here it wants."""
		if not self._accept_from(origin) or origin != self.peer_id:
			return
		if self.role != ROLE_PUBLISHER or not self.active or not isinstance(excluded, list):
			return
		self._excluded = {
			name.strip().lower() for name in excluded[:_MAX_EXCLUSIONS]
			if isinstance(name, str) and name.strip()
		}
		logger.info(
			"Remote audio, the other computer no longer wants to hear: %s",
			", ".join(sorted(self._excluded)) or "nothing",
		)
		self._drop_unwanted()

	def _accept_from(self, origin):
		if origin is None:
			logger.debug("Discarding a remote audio message without an origin")
			return False
		return True

	# Sending.

	def _send(self, message_type, target=None, **kwargs):
		if target is None:
			target = self.peer_id
		try:
			self.transport.send(type=message_type, target=target, **kwargs)
		except Exception:
			logger.exception("Unable to send the %s remote audio message", message_type)


def _refusal_message(reason):
	if reason == "declined":
		# Translators: message spoken when the other computer refused to share its sound
		return _("The other computer refused to share its sound")
	if reason == "busy":
		# Translators: message spoken when the other computer is already sharing its sound
		return _("The other computer is already sharing its sound")
	# Translators: message spoken when the other computer cannot share its sound
	return _("The other computer is unable to share its sound")
