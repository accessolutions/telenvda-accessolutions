import importlib.util
from pathlib import Path
import sys
import types


MODULE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient/keep_awake.py"


def _load_keep_awake(monkeypatch):
	package = types.ModuleType("remoteClient")
	package.__path__ = [str(MODULE_PATH.parent)]
	monkeypatch.setitem(sys.modules, "remoteClient", package)
	monkeypatch.setitem(sys.modules, "speech", types.SimpleNamespace())
	monkeypatch.setitem(sys.modules, "wx", types.SimpleNamespace())
	monkeypatch.setitem(
		sys.modules,
		"logHandler",
		types.SimpleNamespace(log=types.SimpleNamespace(exception=lambda *args: None, debug=lambda *args: None)),
	)
	monkeypatch.setitem(
		sys.modules,
		"remoteClient.configuration",
		types.SimpleNamespace(get_config=lambda: {"keep_awake": {"enabled": False}}),
	)
	spec = importlib.util.spec_from_file_location("remoteClient.keep_awake", MODULE_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def test_only_two_recent_injected_f15_events_are_consumed(monkeypatch):
	keep_awake = _load_keep_awake(monkeypatch)
	now = [10.0]
	monkeypatch.setattr(keep_awake.time, "monotonic", lambda: now[0])
	user32 = types.SimpleNamespace(
		MapVirtualKeyW=lambda *args: 0,
		keybd_event=lambda *args: None,
	)
	monkeypatch.setattr(keep_awake.ctypes, "windll", types.SimpleNamespace(user32=user32), raising=False)
	manager = keep_awake.KeepAwake()

	manager._send_f15()

	assert manager.consume_injected_key(keep_awake.VK_F15 - 1) is False
	assert manager.consume_injected_key(keep_awake.VK_F15) is True
	assert manager.consume_injected_key(keep_awake.VK_F15) is True
	assert manager.consume_injected_key(keep_awake.VK_F15) is False

	manager._send_f15()
	now[0] = 11.5
	assert manager.consume_injected_key(keep_awake.VK_F15) is False
