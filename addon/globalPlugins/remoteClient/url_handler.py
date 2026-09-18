try:
	from logHandler import log
except ImportError:
	from logging import getLogger

	log = getLogger("url_handler")

import ctypes
import ctypes.wintypes
import os
from winUser import WM_COPYDATA  # provided by NVDA
from . import regobj
from . import connection_info

import globalVars
import windowUtils
import wx
import gui  # provided by NVDA
import addonHandler

try:
	addonHandler.initTranslation()
except addonHandler.AddonError:
	log.warning(
		"Unable to initialise translations. This may be because the addon is running from NVDA scratchpad.",
	)


class COPYDATASTRUCT(ctypes.Structure):
	_fields_ = [
		("dwData", ctypes.wintypes.LPARAM),
		("cbData", ctypes.wintypes.DWORD),
		("lpData", ctypes.c_void_p),
	]


PCOPYDATASTRUCT = ctypes.POINTER(COPYDATASTRUCT)

MSGFLT_ALLOW = 1

#: Name of the nvdaHelperLocal function pointer NVDA uses to deliver nvdaremote:// URLs.
#: It only exists on NVDA 2025.1 and later, which is when Remote Access became built in.
_NVDA_URL_POINTER_NAME = "_nvdaControllerInternal_handleRemoteURL"

#: Called with a ConnectionInfo whenever a telenvda:// or nvdaremote:// URL is opened.
_url_callback = None

#: Value of the NVDA function pointer we replaced, so that it can be put back.
_original_url_pointer = None


def _handle_url(url, callback):
	log.info("Received url: %s" % url)
	try:
		con_info = connection_info.ConnectionInfo.from_url(url)
	except connection_info.URLParsingError:
		wx.CallLater(
			50,
			gui.messageBox,
			parent=gui.mainFrame,
			caption=_("Invalid URL"),
			# Translators: Message shown when an invalid URL has been provided.
			message=_('Unable to parse url "%s"') % url,
			style=wx.OK | wx.ICON_ERROR,
		)
		log.exception("unable to parse nvdaremote:// url %s" % url)
		raise
	log.info("Connection info: %r" % con_info)
	if callable(callback):
		wx.CallLater(50, callback, con_info)


class URLHandlerWindow(windowUtils.CustomWindow):
	className = "TeleNVDAURLHandler"

	def __init__(self, callback=None, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.callback = callback
		try:
			ctypes.windll.user32.ChangeWindowMessageFilterEx(self.handle, WM_COPYDATA, MSGFLT_ALLOW, None)
		except AttributeError:
			pass

	def windowProc(self, hwnd, msg, wParam, lParam):
		if msg != WM_COPYDATA:
			return
		hwnd = wParam
		struct_pointer = lParam
		message_data = ctypes.cast(struct_pointer, PCOPYDATASTRUCT)
		url = ctypes.wstring_at(message_data.contents.lpData)
		_handle_url(url, self.callback)


def _handle_url_from_nvda(url):
	try:
		_handle_url(url, _url_callback)
	except connection_info.URLParsingError:
		pass


@ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_wchar_p)
def _nvda_url_callback(url):
	# Called from one of NVDA's RPC threads, so the work is handed to the main thread.
	try:
		wx.CallAfter(_handle_url_from_nvda, url)
	except Exception:
		log.exception("Unable to queue the handling of url %s" % url)
		return -1
	return 0


def _nvda_url_pointer():
	"""Return the nvdaHelperLocal pointer NVDA uses to deliver nvdaremote:// URLs.

	Returns None on the NVDA versions which have no built-in Remote Access.
	"""
	try:
		import NVDAHelper
	except ImportError:
		return None
	local_lib = getattr(NVDAHelper, "localLib", None)
	# NVDA 2025.2 moved the nvdaHelperLocal bindings into a submodule of NVDAHelper.
	dll = getattr(local_lib, "dll", local_lib)
	if dll is None:
		return None
	try:
		symbol = getattr(dll, _NVDA_URL_POINTER_NAME)
	except AttributeError:
		return None
	return ctypes.cast(symbol, ctypes.POINTER(ctypes.c_void_p))


def _install_nvda_url_hook():
	global _original_url_pointer
	pointer = _nvda_url_pointer()
	if pointer is None:
		return False
	if _original_url_pointer is None:
		_original_url_pointer = pointer.contents.value
	pointer.contents.value = ctypes.cast(_nvda_url_callback, ctypes.c_void_p).value
	return True


def _remove_nvda_url_hook():
	global _original_url_pointer
	if _original_url_pointer is None:
		return
	pointer = _nvda_url_pointer()
	if pointer is not None:
		pointer.contents.value = _original_url_pointer
	_original_url_pointer = None


def register_url_handler(callback=None):
	global _url_callback
	_url_callback = callback
	if not _install_nvda_url_hook():
		log.warning(
			"This version of NVDA has no built-in URL handler: "
			"telenvda:// and nvdaremote:// links are unavailable.",
		)
		# Drop any registration left by an older version, which pointed to a bundled executable.
		unregister_url_handler()
		return
	regobj.HKCU.SOFTWARE.Classes.nvdaremote = URL_HANDLER_REGISTRY
	regobj.HKCU.SOFTWARE.Classes.telenvda = URL_HANDLER_REGISTRY


def unregister_url_handler():
	global _url_callback
	_remove_nvda_url_hook()
	_url_callback = None
	for scheme in ("nvdaremote", "telenvda"):
		try:
			delattr(regobj.HKCU.SOFTWARE.Classes, scheme)
		except Exception:
			log.warning("Unable to remove the %s URL handler" % scheme)


def url_handler_path():
	"""Return the helper NVDA itself uses to forward URLs to the running instance.

	The add-on deliberately ships no executable of its own.
	"""
	return os.path.join(globalVars.appDir, "nvda_slave.exe")


URL_HANDLER_REGISTRY = {
	"URL Protocol": "",
	"shell": {
		"open": {
			"command": {
				"": '"{path}" handleRemoteURL %1'.format(path=url_handler_path()),
			},
		},
	},
}
