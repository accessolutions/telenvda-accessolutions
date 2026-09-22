"""Daily log files for the relay traffic, the errors and the alerts of TeleNVDA.

NVDA rewrites its own log at every start and mixes every component into it, which
makes it of little help when a remote assistance session misbehaved hours or days
earlier. This module keeps one file per day in the ``log`` directory of the add-on,
holding the WebSocket and HTTPS traffic along with the warnings and errors raised
by TeleNVDA.

Only the direction, the type and the size of the relay messages are recorded. Their
payload carries speech, braille, keystrokes and clipboard contents, which have no
place in a file kept for weeks.
"""

import datetime
import glob
import logging
import os
import threading

import globalVars


#: Directory created inside the add-on directory to hold the daily files.
LOG_DIR_NAME = "log"
#: Directory used in the NVDA user configuration when the add-on one is read-only.
FALLBACK_DIR_NAME = "teleNVDA-log"
FILE_PREFIX = "telenvda-"
FILE_SUFFIX = ".log"
DEFAULT_RETENTION_DAYS = 30
#: Name of the private logger, kept out of the NVDA log to avoid duplicating traffic there.
LOGGER_NAME = "telenvda.session"

INBOUND = "in"
OUTBOUND = "out"

#: Loggers of the add-on which are not children of the NVDA one, plus the bundled
#: WebSocket library. Their warnings and errors belong to this log as well.
ADDON_LOGGER_NAMES = frozenset(
	(
		"callback_manager",
		"capabilities",
		"file_transfer",
		"keyboard_hook",
		"local_bridge",
		"local_machine",
		"mouse_control",
		"mouse_hook",
		"serializer",
		"session",
		"transport",
		"url_handler",
		"websocket",
	),
)

#: Loggers whose level is lowered while this log is open. NVDA never sets a level on
#: the loggers of an add-on, so they inherit the one of the root logger, which throws
#: away everything below a warning: a connection which kept failing said nothing at
#: all. The traffic stays out of the NVDA log, only these few modules speak louder.
VERBOSE_LOGGER_NAMES = frozenset(
	(
		"capabilities",
		"file_transfer",
		"local_bridge",
		"serializer",
		"session",
		"transport",
		"url_handler",
	),
)

_lock = threading.RLock()
_handler = None
_traffic_enabled = False
_restored_levels = {}


class DailyFileHandler(logging.Handler):
	"""Write records to one file per day and drop the files which grew too old."""

	def __init__(self, directory, retention_days=DEFAULT_RETENTION_DAYS):
		super().__init__()
		self.directory = directory
		self.retention_days = max(0, int(retention_days))
		self._date = None
		self._stream = None

	def file_path(self, date):
		return os.path.join(self.directory, "%s%s%s" % (FILE_PREFIX, date.isoformat(), FILE_SUFFIX))

	def emit(self, record):
		try:
			stream = self._stream_for(datetime.date.fromtimestamp(record.created))
			stream.write(self.format(record) + "\n")
			stream.flush()
		except Exception:
			self.handleError(record)

	def _stream_for(self, date):
		if self._stream is not None and self._date == date:
			return self._stream
		self._close_stream()
		os.makedirs(self.directory, exist_ok=True)
		self._stream = open(self.file_path(date), "a", encoding="utf-8")
		self._date = date
		self.purge_old_files(date)
		return self._stream

	def purge_old_files(self, today=None):
		if not self.retention_days:
			return
		cutoff = (today or datetime.date.today()) - datetime.timedelta(days=self.retention_days)
		for path in glob.glob(os.path.join(self.directory, "%s*%s" % (FILE_PREFIX, FILE_SUFFIX))):
			name = os.path.basename(path)[len(FILE_PREFIX) : -len(FILE_SUFFIX)]
			try:
				file_date = datetime.date.fromisoformat(name)
			except ValueError:
				continue
			if file_date < cutoff:
				try:
					os.remove(path)
				except OSError:
					pass

	def _close_stream(self):
		if self._stream is not None:
			try:
				self._stream.close()
			finally:
				self._stream = None
				self._date = None

	def close(self):
		with _lock:
			self._close_stream()
		super().close()


class AddonAlertFilter(logging.Filter):
	"""Keep the traffic of this log plus the warnings and errors raised by TeleNVDA.

	The handler is attached to the root logger so that everything the add-on reports,
	through the NVDA log or through its own loggers, ends up here. Records coming from
	NVDA itself or from another add-on are discarded: this file is about TeleNVDA.
	"""

	def __init__(self, addon_directory):
		super().__init__()
		self.prefix = os.path.normcase(os.path.abspath(addon_directory)) + os.sep

	def filter(self, record):
		if record.name == LOGGER_NAME:
			return True
		if record.name in ADDON_LOGGER_NAMES:
			# These loggers belong to the add-on alone, so everything they have to say
			# is kept, down to the debug messages telling which address was dialled.
			return True
		if record.levelno < logging.INFO:
			return False
		# NVDA logs everything through a single logger and reports the real origin of a
		# message in its own "codepath" attribute rather than in the standard one.
		if "remoteclient" in str(getattr(record, "codepath", "")).casefold():
			return True
		pathname = getattr(record, "pathname", "") or ""
		return os.path.normcase(os.path.abspath(pathname)).startswith(self.prefix)


def addon_directory():
	"""Return the directory the add-on is installed in."""
	try:
		import addonHandler

		path = getattr(addonHandler.getCodeAddon(), "path", None)
		if path:
			return os.path.abspath(path)
	except Exception:
		# Running from the scratchpad, or too early for the add-on to be known.
		pass
	return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


def _addon_version():
	"""Return the version of the add-on, which a log read months later needs."""
	try:
		import addonHandler

		return addonHandler.getCodeAddon().manifest["version"]
	except Exception:
		return "unknown"


def candidate_directories():
	"""Return the directories which may hold the daily files, most wanted first."""
	directories = [os.path.join(addon_directory(), LOG_DIR_NAME)]
	config_path = getattr(globalVars.appArgs, "configPath", "") or ""
	if config_path:
		directories.append(os.path.join(os.path.abspath(config_path), FALLBACK_DIR_NAME))
	return directories


def _is_writable(directory):
	probe = os.path.join(directory, ".write-test")
	try:
		os.makedirs(directory, exist_ok=True)
		with open(probe, "a", encoding="utf-8"):
			pass
		os.remove(probe)
	except OSError:
		return False
	return True


def resolve_directory():
	"""Return the first writable candidate directory, or None when there is none.

	An add-on installed in a directory the user cannot write to, which happens with
	some managed deployments, falls back to the NVDA user configuration directory
	rather than losing its log altogether.
	"""
	for directory in candidate_directories():
		if _is_writable(directory):
			return directory
	return None


def _report(message):
	"""Say in the NVDA log what this log is doing, or why it is doing nothing.

	Without this, an add-on whose directory cannot be written to simply had no log
	and no way of telling why.
	"""
	try:
		from logHandler import log as nvda_log

		nvda_log.info(message)
	except Exception:
		logging.getLogger(LOGGER_NAME).info(message)


def _set_verbose_levels():
	"""Lower the level of the add-on loggers, remembering what it was."""
	for name in VERBOSE_LOGGER_NAMES:
		logger = logging.getLogger(name)
		_restored_levels[name] = logger.level
		logger.setLevel(logging.DEBUG)


def _restore_levels():
	for name, level in _restored_levels.items():
		logging.getLogger(name).setLevel(level)
	_restored_levels.clear()


def _read_settings():
	from . import configuration

	try:
		return configuration.get_logging_settings()
	except Exception:
		return {
			"enabled": True,
			"traffic": True,
			"retention_days": DEFAULT_RETENTION_DAYS,
		}


def initialize(settings=None, directory=None):
	"""Start writing the daily log, and return whether it is being written.

	Nothing is written on a secure screen: its configuration lives in a temporary
	profile and its content must not survive the session.
	"""
	global _handler, _traffic_enabled
	with _lock:
		if _handler is not None:
			return True
		if getattr(globalVars.appArgs, "secure", False):
			_report("TeleNVDA writes no daily log on a secure screen")
			return False
		if settings is None:
			settings = _read_settings()
		if not settings.get("enabled", True):
			_report("The TeleNVDA daily log is turned off in teleNVDA.ini")
			return False
		if directory is None:
			directory = resolve_directory()
		if directory is None:
			_report(
				"TeleNVDA cannot write its daily log: none of %s can be written to"
				% ", ".join(candidate_directories()),
			)
			return False
		handler = DailyFileHandler(directory, settings.get("retention_days", DEFAULT_RETENTION_DAYS))
		handler.setFormatter(
			logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"),
		)
		handler.addFilter(AddonAlertFilter(addon_directory()))
		logger = logging.getLogger(LOGGER_NAME)
		logger.setLevel(logging.DEBUG)
		# The traffic is verbose and of no use in the NVDA log, which already holds the
		# warnings and the errors this handler picks up from the root logger.
		logger.propagate = False
		logger.addHandler(handler)
		logging.getLogger().addHandler(handler)
		_set_verbose_levels()
		_handler = handler
		_traffic_enabled = bool(settings.get("traffic", True))
	log_connection(
		"log_started",
		directory=directory,
		traffic="on" if _traffic_enabled else "off",
		version=_addon_version(),
	)
	_report("TeleNVDA writes its daily log in %s" % directory)
	return True


def terminate():
	"""Stop writing the daily log and release its file."""
	global _handler, _traffic_enabled
	with _lock:
		handler = _handler
		if handler is None:
			return
		log_connection("log_stopped")
		_handler = None
		_traffic_enabled = False
		_restore_levels()
		logging.getLogger().removeHandler(handler)
		logging.getLogger(LOGGER_NAME).removeHandler(handler)
		handler.close()


def is_enabled():
	return _handler is not None


def traffic_enabled():
	return _traffic_enabled


def directory():
	"""Return the directory the daily files are written to, or None."""
	handler = _handler
	return handler.directory if handler is not None else None


def _format_details(details):
	return " ".join("%s=%s" % (key, value) for key, value in details.items() if value is not None)


def _write(level, category, message):
	if _handler is None:
		return
	logging.getLogger(LOGGER_NAME).log(level, "%s %s" % (category, message))


def log_connection(event, **details):
	"""Record a relay connection event: opening, handshake, failure or closing."""
	_write(logging.INFO, "connection", ("%s %s" % (event, _format_details(details))).strip())


def log_traffic(direction, message_type, size=None, **details):
	"""Record one relay message, by direction, type and size, never by content."""
	if not _traffic_enabled:
		return
	fields = {"bytes": size}
	fields.update(details)
	_write(
		logging.DEBUG,
		"traffic",
		("%s type=%s %s" % (direction, message_type, _format_details(fields))).strip(),
	)


def log_alert(message, **details):
	"""Record an alert which NVDA would not report through its own log."""
	_write(logging.WARNING, "alert", ("%s %s" % (message, _format_details(details))).strip())
