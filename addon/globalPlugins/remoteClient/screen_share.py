"""Screen sharing of the controlled computer, over a peer to peer WebRTC link.

The add-on itself never encodes or decodes video: it drives a helper program which
owns the WebRTC session, captures the screen on the controlled computer and shows
it on the controlling one. NVDA only carries the signalling messages needed to set
that link up, through the relay both computers are already connected to.

Two reasons make the helper mandatory rather than convenient. A WebRTC stack such
as aiortc cannot be vendored in the add-on, because NVDA ships three different
Python ABIs and no wheel exists for the oldest one. Video encoding in the same
process as NVDA would also compete with speech for the interpreter lock, which is
unacceptable for a screen reader.

Everything here degrades gracefully. When the helper is missing, or when screen
sharing is turned off in the configuration, :func:`is_available` returns False,
the feature is never announced, and the add-on behaves exactly as before.
"""

import json
import os
import subprocess
import threading
from logging import getLogger

import wx

import addonHandler
import gui
import ui

from . import capabilities, configuration
from .transport import TransportEvents

logger = getLogger("screen_share")

try:
	addonHandler.initTranslation()
except addonHandler.AddonError:
	logger.warning("Unable to initialise translations. This may be because the addon is running from NVDA scratchpad.")

#: Folder of the add-on holding the helper program.
HELPER_SUBDIR = "helpers"

#: Name of the helper program, which is shipped separately from the add-on itself.
HELPER_EXECUTABLE = "telenvda_screenshare.exe"

#: Role played by this computer during a session.
ROLE_PUBLISHER = "publisher"  # The controlled computer, which captures its screen.
ROLE_VIEWER = "viewer"  # The controlling computer, which displays the picture.

#: States a session goes through.
STATE_IDLE = "idle"
STATE_REQUESTING = "requesting"  # A request was sent, the peer has not answered yet.
STATE_CONNECTING = "connecting"  # Both sides agreed, the WebRTC link is being set up.
STATE_ACTIVE = "active"  # Pictures are flowing.

#: Signalling messages exchanged between the two clients.
MSG_REQUEST = "screen_share_request"
MSG_RESPONSE = "screen_share_response"
MSG_STOP = "screen_share_stop"
MSG_OFFER = "webrtc_offer"
MSG_ANSWER = "webrtc_answer"
MSG_CANDIDATE = "webrtc_candidate"

#: Message asking the relay for temporary TURN credentials.
MSG_TURN_CREDENTIALS = "turn_credentials"

#: Longest session description or ICE candidate accepted from a peer. A malicious
#: or broken peer must not be able to make the helper allocate unbounded memory.
MAX_SIGNALING_PAYLOAD = 64 * 1024


def get_helper_path():
	"""Return the absolute path of the helper program, or None when it is absent."""
	path = os.path.join(os.path.abspath(os.path.dirname(__file__)), HELPER_SUBDIR, HELPER_EXECUTABLE)
	return path if os.path.isfile(path) else None


def is_enabled():
	"""Whether the user left screen sharing turned on."""
	try:
		return bool(configuration.get_config()["screen_share"]["enabled"])
	except Exception:
		logger.debug("Unable to read the screen sharing configuration", exc_info=True)
		return False


def is_available():
	"""Whether this installation can take part in a screen sharing session."""
	return is_enabled() and get_helper_path() is not None


def is_input_control_allowed():
	"""Whether this computer accepts to be driven with the remote mouse."""
	try:
		return bool(configuration.get_config()["screen_share"]["allow_remote_input"])
	except Exception:
		logger.debug("Unable to read the remote input configuration", exc_info=True)
		return False


def _requires_confirmation():
	try:
		return bool(configuration.get_config()["screen_share"]["require_confirmation"])
	except Exception:
		# Asking is the safe default: never share a screen silently.
		return True


def _capture_settings():
	try:
		section = configuration.get_config()["screen_share"]
		return {"max_fps": int(section["max_fps"]), "quality": str(section["quality"])}
	except Exception:
		logger.debug("Unable to read the capture settings", exc_info=True)
		return {"max_fps": 15, "quality": "balanced"}


class ScreenShareHelper:
	"""The helper process, driven with one JSON object per line on its standard streams.

	Commands are written to its standard input, events are read from its standard
	output. Keeping the exchange line based means the helper can be rewritten in any
	language without touching the add-on.
	"""

	def __init__(self, on_event, on_exit):
		self._on_event = on_event
		self._on_exit = on_exit
		self._process = None
		self._reader = None
		self._write_lock = threading.Lock()

	@property
	def running(self):
		return self._process is not None and self._process.poll() is None

	def start(self, role):
		path = get_helper_path()
		if path is None:
			raise RuntimeError("The screen sharing helper is not installed")
		if self.running:
			return
		# The helper is always started from its absolute path, with no shell and with
		# arguments passed as a list, so that nothing from the environment can be
		# substituted for it.
		self._process = subprocess.Popen(
			[path, "--role", role],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			cwd=os.path.dirname(path),
			creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
		)
		self._reader = threading.Thread(target=self._read_events, name="screen_share_helper", daemon=True)
		self._reader.start()

	def send(self, **command):
		"""Send one command to the helper, ignoring a helper which already stopped."""
		if not self.running:
			return
		line = json.dumps(command).encode("utf-8") + b"\n"
		with self._write_lock:
			try:
				self._process.stdin.write(line)
				self._process.stdin.flush()
			except (OSError, ValueError):
				logger.debug("The screen sharing helper closed its input", exc_info=True)

	def stop(self):
		process, self._process = self._process, None
		if process is None:
			return
		try:
			if process.poll() is None:
				try:
					process.stdin.write(b'{"command": "stop"}\n')
					process.stdin.flush()
				except (OSError, ValueError):
					pass
				try:
					process.wait(timeout=3)
				except subprocess.TimeoutExpired:
					logger.warning("The screen sharing helper did not stop, killing it")
					process.kill()
		except Exception:
			logger.exception("Unable to stop the screen sharing helper")

	def _read_events(self):
		process = self._process
		if process is None:
			return
		try:
			for line in process.stdout:
				line = line.strip()
				if not line:
					continue
				try:
					event = json.loads(line.decode("utf-8"))
				except (ValueError, UnicodeDecodeError):
					logger.warning("Discarding an unreadable event from the screen sharing helper")
					continue
				if not isinstance(event, dict):
					continue
				try:
					self._on_event(event)
				except Exception:
					logger.exception("Error while handling a screen sharing helper event")
		except (OSError, ValueError):
			logger.debug("The screen sharing helper closed its output", exc_info=True)
		finally:
			if self._process is process:
				# The helper died on its own rather than being stopped by us.
				try:
					self._on_exit()
				except Exception:
					logger.exception("Error while handling the end of the screen sharing helper")


class ScreenShareManager:
	"""Drive a screen sharing session and carry its signalling over the relay."""

	def __init__(self, transport, negotiator, role):
		self.transport = transport
		self.negotiator = negotiator
		self.role = role
		self.state = STATE_IDLE
		#: Identifier of the peer this session is held with.
		self.peer_id = None
		self.helper = ScreenShareHelper(self._handle_helper_event, self._handle_helper_exit)
		#: ICE servers given by the relay, used as a fallback when a direct link fails.
		self.ice_servers = []
		callbacks = transport.callback_manager
		callbacks.register_callback("msg_" + MSG_REQUEST, self.handle_request)
		callbacks.register_callback("msg_" + MSG_RESPONSE, self.handle_response)
		callbacks.register_callback("msg_" + MSG_STOP, self.handle_stop)
		callbacks.register_callback("msg_" + MSG_OFFER, self.handle_offer)
		callbacks.register_callback("msg_" + MSG_ANSWER, self.handle_answer)
		callbacks.register_callback("msg_" + MSG_CANDIDATE, self.handle_candidate)
		callbacks.register_callback("msg_" + MSG_TURN_CREDENTIALS, self.handle_turn_credentials)
		# Both ends need the ICE servers before a session begins, and the controlled one
		# has no time to ask for them once a request arrives, so they are fetched as soon
		# as the channel is joined.
		callbacks.register_callback(TransportEvents.CONNECTED, self._on_connected)
		# A session cannot outlive the link it is signalled over.
		callbacks.register_callback(TransportEvents.DISCONNECTED, self.terminate)

	# Session control.

	def _on_connected(self):
		if is_available():
			self._request_turn_credentials()

	@property
	def active(self):
		return self.state != STATE_IDLE

	def toggle(self):
		"""Start the session when there is none, stop the current one otherwise.

		Returns a message to report to the user.
		"""
		if self.active:
			self.stop()
			# Translators: message spoken when screen sharing is turned off
			return _("Screen sharing stopped")
		return self.start()

	def start(self):
		"""Ask the controlled computer to share its screen. Returns a message to report."""
		if self.role != ROLE_VIEWER:
			# Translators: message spoken when screen sharing is requested from the wrong computer
			return _("Screen sharing can only be started from the controlling computer")
		if not is_available():
			# Translators: message spoken when the screen sharing helper is missing
			return _("Screen sharing is not available on this computer")
		peers = self.negotiator.peers_supporting(capabilities.FEATURE_SCREEN_SHARE)
		if not peers:
			# Translators: message spoken when the other computer cannot share its screen
			return _("The other computer does not support screen sharing")
		self.peer_id = peers[0]
		self.state = STATE_REQUESTING
		# The relay only hands out TURN credentials to clients which asked for them,
		# and they expire, so they are requested for each session rather than kept.
		self._request_turn_credentials()
		self._send(MSG_REQUEST, allow_input=is_input_control_allowed())
		# Translators: message spoken when screen sharing has been requested
		return _("Screen sharing requested")

	def stop(self, notify_peer=True):
		"""End the current session, telling the peer about it unless it asked for it."""
		if not self.active:
			return
		if notify_peer and self.peer_id is not None:
			self._send(MSG_STOP)
		self.state = STATE_IDLE
		self.peer_id = None
		self.ice_servers = []
		self.helper.stop()

	def terminate(self):
		"""Release everything, when the session or NVDA itself is going away."""
		self.stop(notify_peer=False)

	# Signalling received from the peer.

	def handle_request(self, origin=None, allow_input=False, **kwargs):
		"""The controlling computer asks this one to share its screen."""
		if not self._accept_from(origin):
			return
		if self.role != ROLE_PUBLISHER or not is_available():
			self._refuse(origin, "unavailable")
			return
		if self.active:
			self._refuse(origin, "busy")
			return
		# Remote input is only ever granted when this computer allows it, whatever the
		# controlling computer asked for.
		allow_input = bool(allow_input) and is_input_control_allowed()
		if _requires_confirmation():
			wx.CallAfter(self._ask_permission, origin, allow_input)
		else:
			self._accept_request(origin, allow_input)

	def _ask_permission(self, origin, allow_input):
		if allow_input:
			# Translators: question asked before sharing this screen, with mouse control
			question = _("The controlling computer asks to see this screen and to control the mouse. Do you accept?")
		else:
			# Translators: question asked before sharing this screen
			question = _("The controlling computer asks to see this screen. Do you accept?")
		answer = gui.messageBox(
			parent=gui.mainFrame,
			# Translators: title of the screen sharing request dialog
			caption=_("Screen sharing request"),
			message=question,
			style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
		)
		if answer == wx.YES:
			self._accept_request(origin, allow_input)
		else:
			self._refuse(origin, "declined")

	def _accept_request(self, origin, allow_input):
		if self.active:
			# The user took long enough to answer that another session started.
			self._refuse(origin, "busy")
			return
		self.peer_id = origin
		self.state = STATE_CONNECTING
		try:
			self.helper.start(ROLE_PUBLISHER)
		except Exception:
			logger.exception("Unable to start the screen sharing helper")
			self.state = STATE_IDLE
			self.peer_id = None
			self._refuse(origin, "unavailable")
			return
		self._send(MSG_RESPONSE, accepted=True, allow_input=allow_input)
		self.helper.send(
			command="start",
			role=ROLE_PUBLISHER,
			allow_input=allow_input,
			ice_servers=self.ice_servers,
			**_capture_settings()
		)
		# Translators: message spoken on the controlled computer when it starts sharing its screen
		ui.message(_("Sharing this screen"))

	def _refuse(self, origin, reason):
		self._send(MSG_RESPONSE, target=origin, accepted=False, reason=reason)

	def handle_response(self, origin=None, accepted=False, allow_input=False, reason="", **kwargs):
		"""The controlled computer answered our request."""
		if not self._accept_from(origin) or origin != self.peer_id:
			return
		if self.state != STATE_REQUESTING:
			return
		if not accepted:
			self.state = STATE_IDLE
			self.peer_id = None
			ui.message(_refusal_message(reason))
			return
		self.state = STATE_CONNECTING
		try:
			self.helper.start(ROLE_VIEWER)
		except Exception:
			logger.exception("Unable to start the screen sharing helper")
			self.stop()
			# Translators: message spoken when the screen sharing helper could not be started
			ui.message(_("Unable to start screen sharing"))
			return
		self.helper.send(
			command="start",
			role=ROLE_VIEWER,
			allow_input=bool(allow_input),
			ice_servers=self.ice_servers,
		)

	def handle_stop(self, origin=None, **kwargs):
		if not self._accept_from(origin) or origin != self.peer_id:
			return
		self.stop(notify_peer=False)
		# Translators: message spoken when the other computer ended screen sharing
		ui.message(_("Screen sharing ended"))

	def handle_offer(self, origin=None, sdp=None, **kwargs):
		self._forward_to_helper(origin, "offer", sdp=sdp)

	def handle_answer(self, origin=None, sdp=None, **kwargs):
		self._forward_to_helper(origin, "answer", sdp=sdp)

	def handle_candidate(self, origin=None, candidate=None, **kwargs):
		self._forward_to_helper(origin, "candidate", candidate=candidate)

	def handle_turn_credentials(self, ice_servers=None, **kwargs):
		"""The relay sent the temporary credentials of its TURN server."""
		if isinstance(ice_servers, list):
			self.ice_servers = ice_servers

	def _forward_to_helper(self, origin, kind, **payload):
		"""Hand a session description or an ICE candidate over to the helper."""
		if not self._accept_from(origin) or origin != self.peer_id:
			return
		if self.state not in (STATE_CONNECTING, STATE_ACTIVE):
			return
		for value in payload.values():
			if not isinstance(value, str) or len(value) > MAX_SIGNALING_PAYLOAD:
				logger.warning("Discarding an oversized or malformed %s", kind)
				return
		self.helper.send(command=kind, **payload)

	def _accept_from(self, origin):
		"""Whether a signalling message really comes from an identified peer.

		The origin is stamped by the relay, so a client cannot claim to be another
		one. A message without an origin comes from a relay too old to be trusted
		for one to one delivery, and is therefore dropped.
		"""
		if origin is None:
			logger.debug("Discarding a screen sharing message without an origin")
			return False
		return True

	# Events coming from the helper.

	def _handle_helper_event(self, event):
		kind = event.get("event")
		if kind == "offer":
			self._send(MSG_OFFER, sdp=event.get("sdp", ""))
		elif kind == "answer":
			self._send(MSG_ANSWER, sdp=event.get("sdp", ""))
		elif kind == "candidate":
			self._send(MSG_CANDIDATE, candidate=event.get("candidate", ""))
		elif kind == "connected":
			self.state = STATE_ACTIVE
			# Translators: message spoken when the screen sharing picture starts flowing
			wx.CallAfter(ui.message, _("Screen sharing started"))
		elif kind == "failed":
			logger.warning("Screen sharing failed: %s", event.get("reason", ""))
			wx.CallAfter(self._report_failure)
		elif kind == "closed":
			wx.CallAfter(self._handle_helper_exit)

	def _handle_helper_exit(self):
		if not self.active:
			return
		self.stop()
		# Translators: message spoken when screen sharing stopped unexpectedly
		wx.CallAfter(ui.message, _("Screen sharing ended"))

	def _report_failure(self):
		self.stop()
		# Translators: message spoken when the screen sharing link could not be established
		ui.message(_("Unable to establish the screen sharing connection"))

	# Sending.

	def _request_turn_credentials(self):
		"""Ask the relay for the credentials of its TURN server.

		This one is answered by the relay itself, so it carries no target.
		"""
		try:
			self.transport.send(type=MSG_TURN_CREDENTIALS)
		except Exception:
			logger.exception("Unable to ask the relay for TURN credentials")

	def _send(self, message_type, target=None, **kwargs):
		"""Send a signalling message to the current peer, or to the relay itself.

		Unlike every other message of the protocol, these are delivered to a single
		client, which the relay picks from the target field.
		"""
		if target is None:
			target = self.peer_id
		try:
			self.transport.send(type=message_type, target=target, **kwargs)
		except Exception:
			logger.exception("Unable to send the %s screen sharing message", message_type)


def _refusal_message(reason):
	if reason == "declined":
		# Translators: message spoken when the other computer refused to share its screen
		return _("The other computer refused to share its screen")
	if reason == "busy":
		# Translators: message spoken when the other computer is already sharing its screen
		return _("The other computer is already sharing its screen")
	# Translators: message spoken when the other computer cannot share its screen
	return _("The other computer is unable to share its screen")
