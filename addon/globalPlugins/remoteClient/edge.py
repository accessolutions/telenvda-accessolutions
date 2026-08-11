"""Finding Microsoft Edge and running it as the video engine of a sharing session.

Edge is the only browser supported. It ships with Windows, updates itself through
Windows Update, and provides everything the add-on would otherwise have to build
and maintain: screen capture, hardware accelerated VP8 and VP9 encoding, the
WebRTC transport, and the picture on the controlling side. NVDA keeps only the
signalling, which is a few kilobytes per session.

The window is always started on a throwaway profile. That matters for more than
tidiness: the page is granted every media permission without being asked, which
is the only way found to suppress the source picker (see below), so it must never
run alongside the browsing data, extensions or open tabs of the user.

Measured on Edge 151.0.4129.72:

* ``--auto-select-desktop-capture-source`` has no effect, whatever source title is
  passed to it, in English or in French. The picker opens regardless. This flag is
  therefore not used.
* ``--use-fake-ui-for-media-stream`` does suppress the picker, and the capture
  starts in under a second. Its cost is that it grants microphone and camera as
  well, which is why the throwaway profile and the single local page matter.
* A window placed at ``-32000,-32000`` is not throttled: the frame rate holds and
  the page timers stay regular. Without the anti throttling flags the average rate
  holds too, but freezes appear, so they are kept.

The real consent is asked by NVDA itself, in an accessible dialog, before any of
this runs. The browser dialog is not usable by someone who cannot see the window
it would open in.
"""

import os
import shutil
import subprocess
import tempfile
import threading
import time
from logging import getLogger

logger = getLogger("edge")

try:
	import winreg
except ImportError:  # Not Windows, which the rest of the add-on already assumes.
	winreg = None

#: Authoritative location of the browser under Windows.
_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"

#: Consulted only when the registry entry is missing or points nowhere.
_KNOWN_PATHS = (
	r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
	r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
)

#: Far enough outside any plausible desktop that the window cannot be seen, while
#: still being a real window with a renderer. Minimising would not do: a minimised
#: window can be restored by accident, and a visible one showing the incoming
#: picture would capture itself.
_OFF_SCREEN_POSITION = "-32000,-32000"

#: How long to keep trying to delete the temporary profile. Edge releases its files
#: a moment after the window closes, so the first attempt usually fails.
_CLEANUP_ATTEMPTS = 10
_CLEANUP_DELAY = 1.0


def find_edge():
	"""Return the absolute path of msedge.exe, or None when Edge is not installed.

	The PATH is deliberately never consulted. Any folder the user can write to could
	hold an msedge.exe, and it would then be started with the flags below, which
	grant media permissions without asking.
	"""
	if winreg is not None:
		for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
			try:
				with winreg.OpenKey(hive, _APP_PATHS_KEY) as key:
					value, _type = winreg.QueryValueEx(key, "")
			except OSError:
				continue
			path = os.path.expandvars(str(value).strip().strip('"'))
			if os.path.isfile(path):
				return path
	for candidate in _KNOWN_PATHS:
		path = os.path.expandvars(candidate)
		if os.path.isfile(path):
			return path
	return None


def is_available():
	"""Whether this computer can take part in a session as far as the browser goes."""
	return find_edge() is not None


def _build_arguments(edge, url, profile, off_screen):
	arguments = [
		edge,
		# One window, no address bar, no tabs, nothing the user could navigate with.
		"--app=" + url,
		"--user-data-dir=" + profile,
		"--no-first-run",
		"--no-default-browser-check",
		"--disable-extensions",
		"--disable-sync",
		"--disable-features=Translate,EdgeCollections",
		# See the module docstring: this is what removes the source picker, and the
		# reason the profile above is throwaway.
		"--use-fake-ui-for-media-stream",
		# Not for the average frame rate, which holds without them, but to suppress
		# the freezes measured on a window that is not on screen.
		"--disable-background-timer-throttling",
		"--disable-backgrounding-occluded-windows",
		"--disable-renderer-backgrounding",
	]
	if off_screen:
		arguments += [
			"--window-position=" + _OFF_SCREEN_POSITION,
			"--window-size=320,240",
		]
	else:
		arguments.append("--window-size=1024,700")
	return arguments


class EdgeWindow:
	"""One browser window, on a profile created and destroyed with it."""

	def __init__(self):
		self._process = None
		self._profile = None

	@property
	def running(self):
		return self._process is not None and self._process.poll() is None

	def start(self, url, off_screen):
		"""Open the given local page. Raises RuntimeError when Edge is missing."""
		edge = find_edge()
		if edge is None:
			raise RuntimeError("Microsoft Edge is not installed")
		if self.running:
			return
		self._profile = tempfile.mkdtemp(prefix="telenvda-screenshare-")
		arguments = _build_arguments(edge, url, self._profile, off_screen)
		self._process = subprocess.Popen(
			arguments,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
		)
		logger.debug("Edge started, off screen: %s", off_screen)

	def stop(self):
		"""Close the window and delete its profile, without blocking the caller."""
		process, self._process = self._process, None
		profile, self._profile = self._profile, None
		if process is not None:
			try:
				if process.poll() is None:
					process.terminate()
			except OSError:
				logger.debug("Unable to terminate Edge", exc_info=True)
		if process is not None or profile is not None:
			thread = threading.Thread(
				target=self._reap,
				args=(process, profile),
				name="edge_cleanup",
				daemon=True,
			)
			thread.start()

	def _reap(self, process, profile):
		"""Wait for the browser to let go of its profile, then remove it."""
		if process is not None:
			try:
				process.wait(timeout=5)
			except subprocess.TimeoutExpired:
				logger.warning("Edge did not close, killing it")
				try:
					process.kill()
				except OSError:
					logger.debug("Unable to kill Edge", exc_info=True)
		if profile is None:
			return
		for _attempt in range(_CLEANUP_ATTEMPTS):
			shutil.rmtree(profile, ignore_errors=True)
			if not os.path.isdir(profile):
				return
			time.sleep(_CLEANUP_DELAY)
		logger.warning("Unable to delete the temporary browser profile %s", profile)
