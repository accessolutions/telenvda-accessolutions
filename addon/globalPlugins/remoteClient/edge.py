"""Finding a Chromium browser and running it as the video engine of a sharing session.

Microsoft Edge is preferred: it ships with Windows, updates itself through Windows
Update, and provides everything the add-on would otherwise have to build and
maintain: screen capture, hardware accelerated VP8 and VP9 encoding, the WebRTC
transport, and the picture on the controlling side. NVDA keeps only the
signalling, which is a few kilobytes per session.

Edge is missing from a few hardened or customised Windows installations, so Google
Chrome and Brave are accepted as fallbacks. They are the same engine and take the
very same command line, so nothing else in the add-on has to know which one ran.

The window is always started on a throwaway profile. That matters for more than
tidiness: on the capturing computer the page is granted every media permission
without being asked, which is the only way found to suppress the source picker
(see below), so it must never run alongside the browsing data, extensions or open
tabs of the user.

Measured on Edge 151.0.4129.72:

* ``--auto-select-desktop-capture-source`` has no effect, whatever source title is
  passed to it, in English or in French. The picker opens regardless. This flag is
  therefore not used.
* ``--use-fake-ui-for-media-stream`` does suppress the picker, and the capture
  starts in under a second. Its cost is that it grants microphone and camera as
  well, which is why the throwaway profile and the single local page matter. The
  browser also answers it with a warning bar across the top of the window, so it
  is passed only on the capturing side, whose window is off screen.
* A window placed at ``-32000,-32000`` is not throttled: the frame rate holds and
  the page timers stay regular. Without the anti throttling flags the average rate
  holds too, but freezes appear, so they are kept.

The real consent is asked by NVDA itself, in an accessible dialog, before any of
this runs. The browser dialog is not usable by someone who cannot see the window
it would open in.
"""

import ctypes
import ctypes.wintypes
import os
import shutil
import subprocess
import tempfile
import threading
import time

# See the same import in screen_share: a logger of its own would write nothing below
# the warning level, and which browser started with which layout is exactly what has
# to be readable when a session shows nothing.
from logHandler import log as logger

try:
	import winreg
except ImportError:  # Not Windows, which the rest of the add-on already assumes.
	winreg = None

#: Authoritative location of an installed browser under Windows.
_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s"

#: Browsers this engine can drive, in order of preference. Each entry gives the
#: name of its executable, used both for the registry lookup and for the fixed
#: locations consulted when the registry says nothing, and the folder it installs
#: itself into under Program Files. All of them are Chromium, so they understand
#: the very same command line.
_BROWSERS = (
	("msedge.exe", r"Microsoft\Edge\Application"),
	("chrome.exe", r"Google\Chrome\Application"),
	("brave.exe", r"BraveSoftware\Brave-Browser\Application"),
)

#: Consulted only when the registry entry is missing or points nowhere. The user
#: local one covers the per user installations Chrome and Brave default to.
_KNOWN_ROOTS = (
	"%ProgramFiles(x86)%",
	"%ProgramFiles%",
	"%LocalAppData%",
)

#: Far enough outside any plausible desktop that the window cannot be seen, while
#: still being a real window with a renderer. Minimising would not do: a minimised
#: window can be restored by accident, and a visible one showing the incoming
#: picture would capture itself.
_OFF_SCREEN_POSITION = "-32000,-32000"

#: Title the signalling page gives to its window, and therefore the title Windows
#: reads on the window the browser opens. Kept in step with the title element of
#: web/screen_share.html.
_WINDOW_TITLE = "TeleNVDA screen sharing"

#: How long the off screen window is watched for appearing, and how often. Chromium
#: creates its window a moment after the process starts, and on a busy computer
#: opening a brand new profile that moment can be a couple of seconds.
_FOCUS_GRACE = 10.0
_FOCUS_INTERVAL = 0.2

#: What is needed to take a window out of the task switcher and off the taskbar.
#: A tool window is by definition an accessory of another one, so the shell leaves
#: it out of both, which is exactly what a window nobody can see should be.
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_APPWINDOW = 0x00040000
_SW_HIDE = 0
_SW_SHOWNOACTIVATE = 4

#: How long to keep trying to delete the temporary profile. Edge releases its files
#: a moment after the window closes, so the first attempt usually fails.
_CLEANUP_ATTEMPTS = 10
_CLEANUP_DELAY = 1.0

_ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(
	ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
)


def _get_window_api():
	"""Return a private user32 binding with the prototypes used to tame a window.

	A binding of its own, as elsewhere in the add-on, so that these prototypes cannot
	interfere with the ones NVDA sets on the binding it uses.
	"""
	user32 = getattr(_get_window_api, "_cached", None)
	if user32 is not None:
		return user32
	user32 = ctypes.WinDLL("user32")
	user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
	user32.GetForegroundWindow.argtypes = []
	user32.EnumWindows.restype = ctypes.wintypes.BOOL
	user32.EnumWindows.argtypes = [_ENUM_WINDOWS_PROC, ctypes.wintypes.LPARAM]
	user32.GetWindowTextW.restype = ctypes.c_int
	user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
	user32.GetWindowLongW.restype = ctypes.c_long
	user32.GetWindowLongW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
	user32.SetWindowLongW.restype = ctypes.c_long
	user32.SetWindowLongW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_long]
	user32.ShowWindow.restype = ctypes.wintypes.BOOL
	user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
	_get_window_api._cached = user32
	return user32


def _foreground_window():
	"""Return the handle of the window that currently has the keyboard, or 0."""
	try:
		return _get_window_api().GetForegroundWindow() or 0
	except Exception:
		logger.debug("Unable to read the foreground window", exc_info=True)
		return 0


def _find_sharing_window(user32):
	"""Return the handle of the off screen sharing window, or 0 while it has yet to open."""
	found = []
	# One character more than the title being looked for, so that a longer title which
	# merely starts the same way does not read as a match.
	buffer = ctypes.create_unicode_buffer(len(_WINDOW_TITLE) + 2)

	@_ENUM_WINDOWS_PROC
	def visit(handle, _parameter):
		user32.GetWindowTextW(handle, buffer, len(buffer))
		if buffer.value == _WINDOW_TITLE:
			found.append(handle)
			return False
		return True

	user32.EnumWindows(visit, 0)
	return found[0] if found else 0


def _tame_off_screen_window(previous):
	"""Keep the invisible window out of the way of the user whose screen is shared.

	Chromium always opens its window in the foreground and always gives it a place in
	the task switcher, even a window placed far outside the desktop, and no command
	line option changes either. Both are a nuisance on the computer being watched:
	the keyboard is taken away from whatever its user was doing, their screen reader
	starts reading them a page they cannot see, and a window they can never reach is
	offered to them every time they press alt and tab.

	So the window is turned into a tool window, which the shell leaves out of the task
	switcher and off the taskbar, and the keyboard is handed back. Only once: someone
	who moves elsewhere while the browser is still starting has made a choice, and
	that choice wins over this one.
	"""
	# Imported here and not at the top of the module: the package imports this one.
	from . import force_window_to_foreground

	try:
		user32 = _get_window_api()
		deadline = time.monotonic() + _FOCUS_GRACE
		while time.monotonic() < deadline:
			time.sleep(_FOCUS_INTERVAL)
			handle = _find_sharing_window(user32)
			if not handle:
				continue
			# The shell reads the style when the window is shown, so it is hidden for the
			# time it takes to change it. The browser is told not to slow a window down
			# for being out of sight, which is what makes this safe for the capture.
			user32.ShowWindow(handle, _SW_HIDE)
			style = user32.GetWindowLongW(handle, _GWL_EXSTYLE)
			user32.SetWindowLongW(
				handle, _GWL_EXSTYLE, (style | _WS_EX_TOOLWINDOW) & ~_WS_EX_APPWINDOW
			)
			user32.ShowWindow(handle, _SW_SHOWNOACTIVATE)
			logger.info("Screen sharing: the invisible window was taken out of the task switcher")
			if previous and force_window_to_foreground(previous):
				logger.info("Screen sharing: the keyboard was given back to the window it was in")
			return
	except Exception:
		logger.debug("Unable to tame the off screen window", exc_info=True)


def _find_one(executable, program_files_subdir):
	"""Return the absolute path of the given browser, or None when it is not installed."""
	if winreg is not None:
		for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
			try:
				with winreg.OpenKey(hive, _APP_PATHS_KEY % executable) as key:
					value, _type = winreg.QueryValueEx(key, "")
			except OSError:
				continue
			path = os.path.expandvars(str(value).strip().strip('"'))
			if os.path.isfile(path):
				return path
	for root in _KNOWN_ROOTS:
		path = os.path.expandvars(os.path.join(root, program_files_subdir, executable))
		if os.path.isfile(path):
			return path
	return None


def find_browser():
	"""Return the absolute path of the browser to use, or None when none is installed.

	The PATH is deliberately never consulted. Any folder the user can write to could
	hold a chrome.exe, and it would then be started with the flags below, which grant
	media permissions without asking.
	"""
	for executable, program_files_subdir in _BROWSERS:
		path = _find_one(executable, program_files_subdir)
		if path is not None:
			return path
	return None


def is_available():
	"""Whether this computer can take part in a session as far as the browser goes."""
	return find_browser() is not None


def _build_arguments(browser, url, profile, off_screen):
	arguments = [
		browser,
		# One window, no address bar, no tabs, nothing the user could navigate with.
		"--app=" + url,
		"--user-data-dir=" + profile,
		"--no-first-run",
		"--no-default-browser-check",
		"--disable-extensions",
		"--disable-sync",
		"--disable-features=Translate,EdgeCollections",
		# Not for the average frame rate, which holds without them, but to suppress
		# the freezes measured on a window that is not on screen.
		"--disable-background-timer-throttling",
		"--disable-backgrounding-occluded-windows",
		"--disable-renderer-backgrounding",
	]
	if off_screen:
		arguments += [
			# See the module docstring: this is what removes the source picker, and the
			# reason the profile above is throwaway. It is only passed on the computer
			# that captures, both because the watching side never calls getDisplayMedia
			# and because the browser answers it with a warning bar across the top of
			# the window, which is precisely the window the watching user is looking at.
			"--use-fake-ui-for-media-stream",
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
		"""Open the given local page. Raises RuntimeError when no browser is installed."""
		browser = find_browser()
		if browser is None:
			raise RuntimeError("No supported browser is installed")
		if self.running:
			return
		# Read before the browser opens, which is the moment the foreground changes.
		previous_focus = _foreground_window() if off_screen else 0
		self._profile = tempfile.mkdtemp(prefix="telenvda-screenshare-")
		arguments = _build_arguments(browser, url, self._profile, off_screen)
		self._process = subprocess.Popen(
			arguments,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
			creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
		)
		logger.info("Screen sharing: %s started, off screen: %s", os.path.basename(browser), off_screen)
		if off_screen:
			threading.Thread(
				target=_tame_off_screen_window,
				args=(previous_focus,),
				name="screen_share_window",
				daemon=True,
			).start()

	def stop(self):
		"""Close the window and delete its profile, without blocking the caller."""
		process, self._process = self._process, None
		profile, self._profile = self._profile, None
		if process is not None:
			try:
				if process.poll() is None:
					process.terminate()
			except OSError:
				logger.debug("Unable to terminate the browser", exc_info=True)
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
				logger.warning("The browser did not close, killing it")
				try:
					process.kill()
				except OSError:
					logger.debug("Unable to kill the browser", exc_info=True)
		if profile is None:
			return
		for _attempt in range(_CLEANUP_ATTEMPTS):
			shutil.rmtree(profile, ignore_errors=True)
			if not os.path.isdir(profile):
				return
			time.sleep(_CLEANUP_DELAY)
		logger.warning("Unable to delete the temporary browser profile %s", profile)
