"""Low level mouse hook, used to watch what the user does with the local mouse.

Only button and wheel events are reported here. Pointer movement is deliberately
left out: it fires hundreds of times per second, and Windows silently removes a low
level hook whose callback is too slow, which would break the hook for everything
else. The controlling computer polls the pointer position on a timer instead, see
:mod:`mouse_control`.
"""

from logging import getLogger

logger = getLogger("mouse_hook")

import ctypes
from ctypes import (
	wintypes,
	Structure,
	c_long,
	c_int,
)

from .input import INJECTED_TAG


HC_ACTION = 0
WH_MOUSE_LL = 14
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E

#: One notch of the wheel, as reported in the high word of mouseData.
WHEEL_DELTA = 120

#: Buttons reported to the callbacks, keyed by the message announcing them.
BUTTON_EVENTS = {
	WM_LBUTTONDOWN: ("left", True),
	WM_LBUTTONUP: ("left", False),
	WM_RBUTTONDOWN: ("right", True),
	WM_RBUTTONUP: ("right", False),
	WM_MBUTTONDOWN: ("middle", True),
	WM_MBUTTONUP: ("middle", False),
}


class MSLLHOOKSTRUCT(Structure):
	_fields_ = [
		("pt", wintypes.POINT),
		("mouseData", wintypes.DWORD),
		("flags", wintypes.DWORD),
		("time", wintypes.DWORD),
		("dwExtraInfo", ctypes.c_size_t),
	]


LRESULT = c_long

LowLevelMouseProc = ctypes.WINFUNCTYPE(LRESULT, c_int, wintypes.LPARAM, wintypes.WPARAM)


def _wheel_notches(mouse_data):
	"""Return how many notches the wheel turned, as a signed number.

	Precision wheels report less than a full notch at a time. Rounding those away
	would make such a wheel scroll nothing at all, so any movement counts for one.
	"""
	delta = (mouse_data >> 16) & 0xFFFF
	if delta >= 0x8000:  # The high word holds a signed value.
		delta -= 0x10000
	if not delta:
		return 0
	notches = (abs(delta) + WHEEL_DELTA - 1) // WHEEL_DELTA
	return notches if delta > 0 else -notches


class MouseHook:
	"""Report the local mouse buttons and wheel to the registered callbacks.

	Callbacks receive keyword arguments describing the event: ``action`` is either
	``"button"`` or ``"wheel"``, buttons come with ``button`` and ``pressed``, and
	wheel events with ``delta`` and ``horizontal``.
	"""

	def __init__(self):
		self.callbacks = list()
		self.proc = LowLevelMouseProc(self.mouse_proc)
		user32 = ctypes.windll.user32
		user32.SetWindowsHookExW.restype = ctypes.c_void_p
		user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
		kernel32 = ctypes.windll.kernel32
		kernel32.GetModuleHandleW.restype = ctypes.c_void_p
		self.handle = user32.SetWindowsHookExW(
			WH_MOUSE_LL, self.proc, kernel32.GetModuleHandleW(None), 0
		)

	def register_callback(self, callback):
		self.callbacks.append(callback)

	def unregister_callback(self, callback):
		try:
			self.callbacks.remove(callback)
		except ValueError:
			pass

	def _notify(self, **event):
		for callback in self.callbacks:
			try:
				callback(**event)
			except Exception:
				logger.exception("Error calling callback %r" % callback)

	def mouse_proc(self, code, wParam, lParam):
		if code < 0 or code != HC_ACTION:
			return ctypes.windll.user32.CallNextHookEx(0, code, wParam, lParam)
		message = int(wParam)
		if message != WM_MOUSEMOVE:
			data = ctypes.cast(
				ctypes.c_void_p(lParam), ctypes.POINTER(MSLLHOOKSTRUCT)
			).contents
			# Events this add-on injected itself must not be reported, otherwise a
			# computer controlling another one would echo back what it receives.
			if data.dwExtraInfo != INJECTED_TAG:
				button = BUTTON_EVENTS.get(message)
				if button is not None:
					self._notify(action="button", button=button[0], pressed=button[1])
				elif message in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
					notches = _wheel_notches(data.mouseData)
					if notches:
						self._notify(
							action="wheel",
							delta=notches,
							horizontal=message == WM_MOUSEHWHEEL,
						)
		return ctypes.windll.user32.CallNextHookEx(0, code, wParam, lParam)

	def free(self):
		if self.handle:
			ctypes.windll.user32.UnhookWindowsHookEx(self.handle)
			self.handle = None
