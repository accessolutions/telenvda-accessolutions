"""Braille symbols of NVDA, wherever the running version keeps them.

NVDA 2026.3 split ``braille`` into a package and moved its extension points and
classes into submodules. The old names still work there, but every access
writes a deprecation warning with a full stack into the NVDA log, and they will
be removed some day. Older versions, NVDA 2025 included, only have the old
names. The new location is therefore tried first, without ever touching the old
name when it exists, so that no version logs anything.
"""

import importlib

import braille
import brailleInput


def _moved(new_module, name, old_module):
	try:
		return getattr(importlib.import_module(new_module), name)
	except (ImportError, AttributeError):
		return getattr(old_module, name)


def extension_point(name):
	"""Return the braille extension point called ``name`` (``pre_writeCells``…)."""
	return _moved("braille.extensions", name, braille)


BrailleDisplayGesture = _moved("braille.display.gesture", "BrailleDisplayGesture", braille)
BrailleInputGesture = _moved("braille.input.gesture", "BrailleInputGesture", brailleInput)


def display_dimensions(**kwargs):
	"""Build a ``DisplayDimensions``, which only exists since NVDA 2025.1."""
	return _moved("braille.display", "DisplayDimensions", braille)(**kwargs)
