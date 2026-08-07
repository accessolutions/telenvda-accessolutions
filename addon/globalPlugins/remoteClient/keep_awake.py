"""Prevent the local computer from going to sleep while it is left unattended.

When no keyboard or mouse activity has been detected for a configurable delay,
speech is temporarily turned off, an F15 key press (a key which does nothing on
Windows) is injected so that the system idle timer is reset, then speech is
restored.
"""

import ctypes
from ctypes import wintypes
import time

import speech
import wx
from logHandler import log

from . import configuration

VK_F15 = 0x7E
KEYEVENTF_KEYUP = 0x0002
MAPVK_VK_TO_VSC = 0

# Delay, in milliseconds, between the injected key press and the restoration of
# the previous speech mode.
_SPEECH_RESTORE_DELAY_MS = 500
# Minimum delay, in seconds, between two idle checks.
_MIN_CHECK_INTERVAL_SECONDS = 1


class LASTINPUTINFO(ctypes.Structure):
	_fields_ = [
		("cbSize", wintypes.UINT),
		("dwTime", wintypes.DWORD),
	]


def get_system_idle_seconds():
	"""Return the number of seconds since the last system wide keyboard or mouse input."""
	info = LASTINPUTINFO()
	info.cbSize = ctypes.sizeof(info)
	if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
		return 0.0
	elapsed = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0
	# GetTickCount wraps around after about 49 days. Treat a negative result as no idle time.
	return elapsed if elapsed > 0 else 0.0


def _get_speech_mode():
	try:
		return speech.getState().speechMode
	except AttributeError:
		return speech.speechMode


def _set_speech_mode(mode):
	try:
		speech.setSpeechMode(mode)
	except AttributeError:
		speech.speechMode = mode


def _speech_mode_off():
	try:
		return speech.SpeechMode.off
	except AttributeError:
		return speech.speechMode_off


class KeepAwake:
	"""Injects an F15 key press when the machine has been left unattended."""

	def __init__(self):
		self._timer = None
		self._restore_timer = None
		self._previous_speech_mode = None
		self._last_local_input = time.monotonic()

	def notify_local_input(self):
		"""Record activity reported by the add-on keyboard and mouse hooks."""
		self._last_local_input = time.monotonic()

	def start(self):
		self.notify_local_input()
		self._schedule()

	def reload(self):
		"""Take the current configuration into account."""
		self._cancel_timer()
		self._schedule()

	def stop(self):
		self._cancel_timer()
		if self._restore_timer is not None:
			self._restore_timer.Stop()
			self._restore_timer = None
		self._restore_speech_mode()

	def _cancel_timer(self):
		if self._timer is not None:
			self._timer.Stop()
			self._timer = None

	def _get_settings(self):
		config = configuration.get_config()['keep_awake']
		return bool(config['enabled']), max(5, int(config['delay_seconds']))

	def _schedule(self, delay_seconds=None):
		try:
			enabled, configured_delay = self._get_settings()
		except Exception:
			log.exception("Unable to read the keep awake configuration")
			return
		if not enabled:
			return
		if delay_seconds is None:
			delay_seconds = configured_delay
		delay_seconds = max(_MIN_CHECK_INTERVAL_SECONDS, delay_seconds)
		self._timer = wx.CallLater(int(delay_seconds * 1000), self._check)

	def _check(self):
		self._timer = None
		try:
			enabled, delay = self._get_settings()
		except Exception:
			log.exception("Unable to read the keep awake configuration")
			return
		if not enabled:
			return
		idle = min(get_system_idle_seconds(), time.monotonic() - self._last_local_input)
		if idle >= delay:
			self._trigger(delay)
			self._schedule(delay)
		else:
			self._schedule(delay - idle)

	def _trigger(self, delay):
		log.debug("Keeping the computer awake after %s seconds without activity" % delay)
		self.notify_local_input()
		if self._previous_speech_mode is None:
			try:
				self._previous_speech_mode = _get_speech_mode()
				_set_speech_mode(_speech_mode_off())
			except Exception:
				log.exception("Unable to turn speech off")
				self._previous_speech_mode = None
		self._send_f15()
		if self._restore_timer is not None:
			self._restore_timer.Stop()
		self._restore_timer = wx.CallLater(_SPEECH_RESTORE_DELAY_MS, self._restore_speech_mode)

	def _send_f15(self):
		try:
			user32 = ctypes.windll.user32
			scan_code = user32.MapVirtualKeyW(VK_F15, MAPVK_VK_TO_VSC)
			user32.keybd_event(VK_F15, scan_code, 0, 0)
			user32.keybd_event(VK_F15, scan_code, KEYEVENTF_KEYUP, 0)
		except Exception:
			log.exception("Unable to send the F15 key")

	def _restore_speech_mode(self):
		self._restore_timer = None
		if self._previous_speech_mode is None:
			return
		mode = self._previous_speech_mode
		self._previous_speech_mode = None
		try:
			_set_speech_mode(mode)
		except Exception:
			log.exception("Unable to restore the previous speech mode")
