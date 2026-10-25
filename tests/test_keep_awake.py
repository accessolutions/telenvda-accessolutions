import contextlib
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
F15_PRESS = ("key", 0x7E)
# Everything a trigger does: Windows idle timers reset, then F15 pressed and released
# while NVDA ignores injected keys.
TRIGGER = [IDLE_RESET, ("ignore_injection", True), F15_PRESS, F15_PRESS, ("ignore_injection", False)]


def _idle_manager(monkeypatch, say_all_running=False, current_activity=None, max_duration=0):
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
		SetThreadExecutionState=lambda flags: actions.append(("idle_reset", flags)),
	)
	monkeypatch.setattr(
		keep_awake.ctypes,
		"windll",
		types.SimpleNamespace(user32=user32, kernel32=kernel32),
		raising=False,
	)

	@contextlib.contextmanager
	def ignore_injection():
		actions.append(("ignore_injection", True))
		yield
		actions.append(("ignore_injection", False))

	monkeypatch.setitem(
		sys.modules,
		"remoteClient.remote_keyboard",
		types.SimpleNamespace(ignore_injection=ignore_injection),
	)
	scheduled = []
	monkeypatch.setattr(
		keep_awake,
		"wx",
		types.SimpleNamespace(
			CallLater=lambda delay, callback: scheduled.append(delay)
			or types.SimpleNamespace(Stop=lambda: None),
			CallAfter=lambda callback: callback(),
		),
	)
	manager = keep_awake.KeepAwake(current_activity=current_activity)
	manager._get_settings = lambda: (True, DELAY, max_duration)
	manager._last_local_input = now[0] - DELAY * 2
	manager.scheduled = scheduled
	return keep_awake, manager, actions, now


def test_idle_computer_gets_f15_hidden_from_nvda_without_touching_speech(monkeypatch):
	keep_awake, manager, actions, now = _idle_manager(monkeypatch)

	manager._check()

	assert actions == TRIGGER
	assert not hasattr(manager, "_previous_speech_mode")
	assert not hasattr(keep_awake, "_set_speech_mode")


def test_f15_is_still_sent_while_nvda_speaks_or_reads(monkeypatch):
	keep_awake, manager, actions, now = _idle_manager(monkeypatch, say_all_running=True)
	manager.notify_speech()
	now[0] += DELAY / 2

	manager._check()

	assert actions == TRIGGER


def test_f15_is_still_sent_during_a_transfer_or_a_sharing(monkeypatch):
	keep_awake, manager, actions, now = _idle_manager(monkeypatch, current_activity=lambda: "a file transfer")

	manager._check()

	assert actions == TRIGGER


def test_activity_restarts_the_maximum_duration(monkeypatch):
	keep_awake, manager, actions, now = _idle_manager(monkeypatch, max_duration=DELAY * 4)
	manager._check()
	started = manager._prevention_started
	now[0] += DELAY * 2

	# Nothing happened on the computer: the clock keeps running.
	manager._check()
	assert manager._prevention_started == started

	# A braille display gesture shows that the computer is in use: the clock restarts.
	now[0] += DELAY
	manager.notify_gesture(gesture=object())
	manager._check()
	assert manager._prevention_started == now[0]
	assert manager._prevention_expired is False


def test_prevention_stops_after_the_maximum_duration_and_resumes_on_input(monkeypatch):
	keep_awake, manager, actions, now = _idle_manager(monkeypatch, max_duration=DELAY * 2)
	manager._check()
	now[0] += DELAY * 2
	del actions[:]

	manager._check()
	assert actions == []
	assert manager._prevention_expired is True

	del manager.scheduled[:]
	manager.notify_local_input()
	assert manager._prevention_expired is False
	assert manager.scheduled, "the idle checks start again once the user is back"


def test_a_broken_injection_bypass_still_sends_f15_and_arms_the_raw_key_filter(monkeypatch):
	keep_awake, manager, actions, now = _idle_manager(monkeypatch)
	monkeypatch.setitem(sys.modules, "remoteClient.remote_keyboard", None)

	manager._check()

	assert actions == [IDLE_RESET, F15_PRESS, F15_PRESS]
	assert manager.consume_injected_key(keep_awake.VK_F15) is True


def test_gesture_handler_lets_nvda_execute_the_gesture(monkeypatch):
	keep_awake = _load_keep_awake(monkeypatch)
	manager = keep_awake.KeepAwake()

	assert manager.notify_gesture(gesture=object()) is True
	assert manager._last_gesture is not None
