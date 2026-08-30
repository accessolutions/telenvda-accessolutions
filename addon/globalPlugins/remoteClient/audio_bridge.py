"""The native helper which captures the sound of this computer without NVDA in it.

The browser can capture the system mix, but only all of it. What this computer plays
includes NVDA, so the sound sent to the watching computer used to carry the remote
NVDA speaking, which is why the forwarded speech had to be silenced while it flowed.

Windows can do better. Since build 20348 an audio client can be activated in process
loopback mode, capturing everything except one process tree. No web API exposes it,
so a small native helper does the capture and hands the samples to the page over a
loopback WebSocket, which turns them back into a WebRTC track.

The helper repeats the protections of local_bridge: it binds to 127.0.0.1 alone, the
port is chosen by the system for each session, every connection must carry the session
token, and an Origin header that is not the page's own is refused. The token goes in
through the standard input of the helper rather than its command line, which every
other process on the machine can read.

When any of this is unavailable - an older Windows, a missing helper, a refusal from
the audio engine - is_available() answers no and the caller falls back to the browser
capture, with the forwarded speech silenced as before.
"""

import base64
import os
import subprocess
import sys
import threading
from logging import getLogger

logger = getLogger("audio_bridge")

#: Build which introduced AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK.
MIN_WINDOWS_BUILD = 20348

#: Where the helper sits, next to this module.
HELPER_SUBDIR = "bin"
HELPER_NAME = "nvda_audio_capture.exe"

#: The helper prints its port as soon as it is listening. It has nothing to do
#: beforehand, so waiting long would only hide a failure.
START_TIMEOUT = 5.0

TOKEN_BYTES = 32


def helper_path():
	"""Return the absolute path of the helper, or None when it is not there."""
	path = os.path.join(os.path.abspath(os.path.dirname(__file__)), HELPER_SUBDIR, HELPER_NAME)
	return path if os.path.isfile(path) else None


def is_available():
	"""Whether this computer can capture its sound with NVDA left out of it."""
	try:
		if sys.getwindowsversion().build < MIN_WINDOWS_BUILD:
			return False
	except Exception:
		logger.debug("Unable to read the Windows build", exc_info=True)
		return False
	return helper_path() is not None


def _make_token():
	"""Return an unguessable token for one session, as local_bridge does."""
	return base64.urlsafe_b64encode(os.urandom(TOKEN_BYTES)).decode("ascii").rstrip("=")


class AudioBridge:
	"""One run of the native helper, serving one page."""

	def __init__(self):
		self._process = None
		self.url = None

	@property
	def running(self):
		return self._process is not None and self._process.poll() is None

	def start(self, origin):
		"""Start capturing and return the address the page must connect to.

		`origin` is the local origin the page is served from, and the only one the
		helper will accept. Returns None when the helper could not be started, which
		is never fatal: the caller falls back to the browser capture.
		"""
		if self.running:
			return self.url
		path = helper_path()
		if path is None or not origin:
			return None
		token = _make_token()
		try:
			self._process = subprocess.Popen(
				[
					path,
					"--serve",
					"--origin",
					origin,
					# Excluding this very process is the whole point. The mode excludes
					# the children too, so anything NVDA starts to speak is covered.
					"--pid",
					str(os.getpid()),
				],
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				stderr=subprocess.DEVNULL,
				creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
			)
			self._process.stdin.write((token + "\n").encode("ascii"))
			self._process.stdin.flush()
		except Exception:
			logger.exception("Unable to start the audio capture helper")
			self.stop()
			return None

		port = self._read_port()
		if port is None:
			logger.error("The audio capture helper did not report a port")
			self.stop()
			return None
		self.url = "ws://127.0.0.1:%d/?token=%s" % (port, token)
		logger.debug("Audio capture helper listening on port %d", port)
		return self.url

	def _read_port(self):
		"""Read the "PORT n" line the helper prints once it listens."""
		result = {}

		def read():
			try:
				result["line"] = self._process.stdout.readline().decode("ascii", "replace")
			except Exception:
				logger.debug("Unable to read from the audio capture helper", exc_info=True)

		reader = threading.Thread(target=read, name="audio_bridge_start", daemon=True)
		reader.start()
		reader.join(START_TIMEOUT)
		line = (result.get("line") or "").strip()
		if not line.startswith("PORT "):
			return None
		try:
			return int(line.split()[1])
		except (IndexError, ValueError):
			return None

	def stop(self):
		"""Stop capturing. Safe to call at any point, including twice."""
		process, self._process = self._process, None
		self.url = None
		if process is None:
			return
		try:
			if process.poll() is None:
				process.terminate()
		except Exception:
			logger.debug("Unable to stop the audio capture helper", exc_info=True)
		for stream in (process.stdin, process.stdout):
			try:
				if stream is not None:
					stream.close()
			except Exception:
				pass
