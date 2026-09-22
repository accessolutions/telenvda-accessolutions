import datetime
import importlib.util
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


PACKAGE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient"
PACKAGE_NAME = "traffic_log_test_remote_client"


def _load_traffic_log():
	"""Load the daily log against a fake NVDA of our own.

	The stub is removed from the module registry once the module is loaded, so that
	the other test modules of this suite keep the NVDA they install themselves.
	"""
	global_vars = ModuleType("globalVars")
	global_vars.appArgs = SimpleNamespace(secure=False, launcher=False, configPath="")
	replaced = sys.modules.get("globalVars")
	sys.modules["globalVars"] = global_vars
	try:
		package = ModuleType(PACKAGE_NAME)
		package.__path__ = [str(PACKAGE_PATH)]
		sys.modules[PACKAGE_NAME] = package
		spec = importlib.util.spec_from_file_location(
			f"{PACKAGE_NAME}.traffic_log",
			PACKAGE_PATH / "traffic_log.py",
		)
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		sys.modules[f"{PACKAGE_NAME}.traffic_log"] = module
		spec.loader.exec_module(module)
	finally:
		if replaced is None:
			del sys.modules["globalVars"]
		else:
			sys.modules["globalVars"] = replaced
	return module, global_vars


traffic_log, globalVars = _load_traffic_log()


@pytest.fixture(autouse=True)
def stopped_log():
	previous = (globalVars.appArgs.secure, getattr(globalVars.appArgs, "configPath", ""))
	globalVars.appArgs.secure = False
	globalVars.appArgs.configPath = ""
	yield
	traffic_log.terminate()
	globalVars.appArgs.secure, globalVars.appArgs.configPath = previous


def _settings(**values):
	settings = {"enabled": True, "traffic": True, "retention_days": 30}
	settings.update(values)
	return settings


def handler_name(date):
	return "%s%s%s" % (traffic_log.FILE_PREFIX, date.isoformat(), traffic_log.FILE_SUFFIX)


def _read(directory, date=None):
	path = Path(directory) / handler_name(date or datetime.date.today())
	return path.read_text(encoding="utf-8") if path.exists() else ""


def _record(created, message="message"):
	record = logging.LogRecord("transport", logging.WARNING, __file__, 1, message, None, None)
	record.created = created
	return record


def test_records_are_written_to_one_file_per_day(tmp_path):
	handler = traffic_log.DailyFileHandler(str(tmp_path))
	handler.setFormatter(logging.Formatter("%(message)s"))

	monday = datetime.datetime(2026, 9, 21, 23, 59, 30)
	tuesday = datetime.datetime(2026, 9, 22, 0, 0, 30)
	handler.emit(_record(monday.timestamp(), "before midnight"))
	handler.emit(_record(tuesday.timestamp(), "after midnight"))
	handler.close()

	assert _read(tmp_path, monday.date()) == "before midnight\n"
	assert _read(tmp_path, tuesday.date()) == "after midnight\n"


def test_files_older_than_the_retention_are_deleted(tmp_path):
	today = datetime.date.today()
	kept = tmp_path / handler_name(today - datetime.timedelta(days=29))
	dropped = tmp_path / handler_name(today - datetime.timedelta(days=31))
	unrelated = tmp_path / "notes.log"
	for path in (kept, dropped, unrelated):
		path.write_text("old", encoding="utf-8")

	handler = traffic_log.DailyFileHandler(str(tmp_path), retention_days=30)
	handler.purge_old_files(today)

	assert kept.exists()
	assert unrelated.exists()
	assert not dropped.exists()


def test_traffic_is_recorded_without_its_payload(tmp_path):
	assert traffic_log.initialize(_settings(), str(tmp_path)) is True

	traffic_log.log_traffic(traffic_log.OUTBOUND, "speak", 213, transport="WebSocketRelayTransport")

	content = _read(tmp_path)
	assert "traffic out type=speak bytes=213 transport=WebSocketRelayTransport" in content


def test_connection_events_are_recorded(tmp_path):
	traffic_log.initialize(_settings(), str(tmp_path))

	traffic_log.log_connection("websocket_opening", url="wss://relay.example/", proxy="none")

	assert "connection websocket_opening url=wss://relay.example/ proxy=none" in _read(tmp_path)


def test_traffic_can_be_turned_off_while_alerts_stay(tmp_path):
	traffic_log.initialize(_settings(traffic=False), str(tmp_path))

	traffic_log.log_traffic(traffic_log.INBOUND, "speak", 42)
	traffic_log.log_alert("the relay refused the key")

	content = _read(tmp_path)
	assert "type=speak" not in content
	assert "alert the relay refused the key" in content


def test_warnings_of_the_addon_are_captured_but_not_those_of_nvda(tmp_path):
	traffic_log.initialize(_settings(), str(tmp_path))

	addon_logger = logging.getLogger("transport")
	addon_logger.setLevel(logging.WARNING)
	addon_logger.warning("HTTPS message delivery returned status 503")
	nvda_logger = logging.getLogger("someOtherComponent")
	nvda_logger.setLevel(logging.WARNING)
	nvda_logger.warning("an unrelated component failed")

	content = _read(tmp_path)
	assert "HTTPS message delivery returned status 503" in content
	assert "an unrelated component failed" not in content


def test_nvda_records_are_recognised_by_their_codepath(tmp_path):
	traffic_log.initialize(_settings(), str(tmp_path))

	logger = logging.getLogger("nvda")
	logger.setLevel(logging.WARNING)
	logger.warning(
		"Unable to connect",
		extra={"codepath": "globalPlugins.remoteClient.session.handleMessage"},
	)

	assert "Unable to connect" in _read(tmp_path)


def test_nothing_is_written_on_a_secure_screen(tmp_path):
	globalVars.appArgs.secure = True

	assert traffic_log.initialize(_settings(), str(tmp_path)) is False
	assert traffic_log.is_enabled() is False


def test_logging_can_be_turned_off_altogether(tmp_path):
	assert traffic_log.initialize(_settings(enabled=False), str(tmp_path)) is False


def test_directory_falls_back_to_the_configuration_when_the_addon_is_read_only(tmp_path, monkeypatch):
	unwritable = tmp_path / "addon"
	globalVars.appArgs.configPath = str(tmp_path / "config")
	monkeypatch.setattr(traffic_log, "addon_directory", lambda: str(unwritable))
	monkeypatch.setattr(
		traffic_log,
		"_is_writable",
		lambda directory: not directory.startswith(str(unwritable)),
	)

	assert traffic_log.resolve_directory() == str(tmp_path / "config" / traffic_log.FALLBACK_DIR_NAME)
