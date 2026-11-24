"""braille_compat finds the braille symbols of every NVDA version without deprecated names."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


MODULE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient/braille_compat.py"
NAMES = (
	"pre_writeCells",
	"filter_displaySize",
	"filter_displayDimensions",
	"displaySizeChanged",
	"displayChanged",
	"decide_enabled",
)


class _Point:
	def __init__(self, name):
		self.name = name


def _load(monkeypatch, modules):
	for name in [n for n in sys.modules if n == "braille" or n.startswith("braille.") or n == "brailleInput"]:
		monkeypatch.delitem(sys.modules, name)
	for name, module in modules.items():
		monkeypatch.setitem(sys.modules, name, module)
	spec = importlib.util.spec_from_file_location("braille_compat_under_test", MODULE_PATH)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _old_layout():
	"""NVDA 2024.1 to 2026.2: ``braille`` is a single module."""
	braille = ModuleType("braille")
	for name in NAMES:
		setattr(braille, name, _Point(name))
	braille.BrailleDisplayGesture = type("BrailleDisplayGesture", (), {})
	braille.DisplayDimensions = lambda **kwargs: ("old", kwargs)
	braille_input = ModuleType("brailleInput")
	braille_input.BrailleInputGesture = type("BrailleInputGesture", (), {})
	return {"braille": braille, "brailleInput": braille_input}


def _new_layout(accessed):
	"""NVDA 2026.3: a package whose old names log a deprecation warning."""

	def deprecated(name):
		accessed.append(name)
		raise AssertionError(f"deprecated name braille.{name} used")

	braille = ModuleType("braille")
	braille.__path__ = []
	braille.__getattr__ = deprecated
	extensions = ModuleType("braille.extensions")
	for name in NAMES:
		setattr(extensions, name, _Point(name))
	display = ModuleType("braille.display")
	display.__path__ = []
	display.DisplayDimensions = lambda **kwargs: ("new", kwargs)
	gesture = ModuleType("braille.display.gesture")
	gesture.BrailleDisplayGesture = type("BrailleDisplayGesture", (), {})
	braille_input_pkg = ModuleType("braille.input")
	braille_input_pkg.__path__ = []
	input_gesture = ModuleType("braille.input.gesture")
	input_gesture.BrailleInputGesture = type("BrailleInputGesture", (), {})
	braille_input = ModuleType("brailleInput")

	def deprecated_input(name):
		accessed.append("brailleInput." + name)
		raise AssertionError(f"deprecated name brailleInput.{name} used")

	braille_input.__getattr__ = deprecated_input
	return {
		"braille": braille,
		"braille.extensions": extensions,
		"braille.display": display,
		"braille.display.gesture": gesture,
		"braille.input": braille_input_pkg,
		"braille.input.gesture": input_gesture,
		"brailleInput": braille_input,
	}


@pytest.mark.parametrize("name", NAMES)
def test_old_nvda_extension_points(monkeypatch, name):
	compat = _load(monkeypatch, _old_layout())
	assert compat.extension_point(name).name == name


def test_old_nvda_classes(monkeypatch):
	modules = _old_layout()
	compat = _load(monkeypatch, modules)
	assert compat.BrailleDisplayGesture is modules["braille"].BrailleDisplayGesture
	assert compat.BrailleInputGesture is modules["brailleInput"].BrailleInputGesture
	assert compat.display_dimensions(numRows=1, numCols=40) == ("old", {"numRows": 1, "numCols": 40})


def test_nvda_2026_3_never_touches_deprecated_names(monkeypatch):
	accessed = []
	modules = _new_layout(accessed)
	compat = _load(monkeypatch, modules)
	for name in NAMES:
		assert compat.extension_point(name) is getattr(modules["braille.extensions"], name)
	assert compat.BrailleDisplayGesture is modules["braille.display.gesture"].BrailleDisplayGesture
	assert compat.BrailleInputGesture is modules["braille.input.gesture"].BrailleInputGesture
	assert compat.display_dimensions(numRows=1, numCols=40) == ("new", {"numRows": 1, "numCols": 40})
	assert accessed == []
