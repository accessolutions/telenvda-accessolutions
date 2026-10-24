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
		types.SimpleNamespace(
			log=types.SimpleNamespace(exception=lambda *args: None, debug=lambda *args: None),
		),
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


DELAY = 60
IDLE_RESET = ("idle_reset", 0x00000001 | 0x00000002)


def _idle_manager(monkeypatch, say_all_running=False):
	"""Return a keep awake whose keyboard and mouse have been idle for twice the delay."""
	keep_awake = _load_keep_awake(monkeypatch)
	now = [1000.0]
	monkeypatch.setattr(keep_awake.time, "monotonic", lambda: now[0])
	monkeypatch.setattr(keep_awake, "get_system_idle_seconds", lambda: DELAY * 2)
	monkeypatch.setitem(
		sys.modules,
		"speech.sayAll",
		types.SimpleNamespace(SayAllHandler=types.SimpleNamespace(isRunning=lambda: say_all_running)),
	)
	actions = []
	user32 = types.SimpleNamespace(
		MapVirtualKeyW=lambda *args: 0,
		keybd_event=lambda vk, *args: actions.append(("key", vk)),
	)
	kernel32 = types.SimpleNamespace(
		SetThreadExecutionState=lambda flags: actions.append(("idle_reset", flags))
	)
	monkeypatch.setattr(
		keep_awake.ctypes,
		"windll",
		types.SimpleNamespace(user32=user32, kernel32=kernel32),
		raising=False,
	)
	monkeypatch.setattr(keep_awake, "_get_speech_mode", lambda: "talk")
	monkeypatch.setattr(keep_awake, "_set_speech_mode", lambda mode: actions.append(("speech", mode)))
	monkeypatch.setattr(keep_awake, "_speech_mode_off", lambda: "off")
	monkeypatch.setattr(
		keep_awake,
		"wx",
		types.SimpleNamespace(CallLater=lambda delay, callback: types.SimpleNamespace(Stop=lambda: None)),
	)
	manager = keep_awake.KeepAwake()
	manager._get_settings = lambda: (True, DELAY, 0)
	manager._last_local_input = now[0] - DELAY * 2
	return keep_awake, manager, actions, now


def test_idle_and_silent_computer_gets_f15_with_speech_off_around_it(monkeypatch):
	keep_awake, manager, actions, now = _idle_manager(monkeypatch)

	manager._check()

	assert actions.index(("speech", "off")) < actions.index(("key", keep_awake.VK_F15))
	manager._restore_speech_mode()
	assert actions[-1] == ("speech", "talk")


def test_recent_speech_keeps_speech_on_and_sends_no_f15(monkeypatch):
	keep_awake, manager, actions, now = _idle_manager(monkeypatch)
	manager.notify_speech()
	now[0] += DELAY / 2

	manager._check()

	assert actions == [IDLE_RESET]


def test_say_all_keeps_speech_on_and_sends_no_f15(monkeypatch):
	keep_awake, manager, actions, now = _idle_manager(monkeypatch, say_all_running=True)

	manager._check()

	assert actions == [IDLE_RESET]


def test_speech_older_than_the_delay_is_not_activity(monkeypatch):
	keep_awake, manager, actions, now = _idle_manager(monkeypatch)
	manager.notify_speech()
	now[0] += DELAY + 1

	manager._check()

	assert ("speech", "off") in actions
	assert ("key", keep_awake.VK_F15) in actions
