from logging import getLogger

logger = getLogger("mouse_hook")

import ctypes
from ctypes import (
	wintypes,
	Structure,
	c_long,
	c_int,
)


HC_ACTION = 0
WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205


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


class MouseHook:
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

	def mouse_proc(self, code, wParam, lParam):
		if code < 0 or code != HC_ACTION:
			return ctypes.windll.user32.CallNextHookEx(0, code, wParam, lParam)
		events = {
			WM_LBUTTONDOWN: ("left", True),
			WM_LBUTTONUP: ("left", False),
			WM_RBUTTONDOWN: ("right", True),
			WM_RBUTTONUP: ("right", False),
		}
		event = events.get(int(wParam))
		if event is not None:
			button, pressed = event
			for callback in self.callbacks:
				try:
					callback(button=button, pressed=pressed)
				except Exception:
					logger.exception("Error calling callback %r" % callback)
		return ctypes.windll.user32.CallNextHookEx(0, code, wParam, lParam)

	def free(self):
		if self.handle:
			ctypes.windll.user32.UnhookWindowsHookEx(self.handle)
			self.handle = None
