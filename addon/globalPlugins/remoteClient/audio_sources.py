"""The applications that are playing sound on this computer.

The capture in :mod:`audio_capture` needs to be told which process to listen to.
Windows knows that under the name of audio sessions: every program that opened the
sound card owns one, and it says which process it belongs to. That is what is read
here, turned into something a person can recognise.

Two details matter and are easy to get wrong.

A browser does not play its sound from the window the user sees but from one of the
processes that window started, so the session found here is answered with the
process at the root of the tree instead. Capturing that root takes the whole tree
with it, which is what someone who asked for "the browser" meant.

A program that has been quiet for a while loses its session and reappears when it
plays again. What is remembered elsewhere is therefore the name of the executable,
which does not change, and never the identifier of the process, which does.
"""

import ctypes
import os
from ctypes import POINTER, byref, c_void_p, sizeof
from ctypes.wintypes import BOOL, DWORD, HANDLE, LONG, MAX_PATH, WCHAR
from logging import getLogger

from .audio_capture import GUID, _CLSID_MMDeviceEnumerator, _IID_IMMDeviceEnumerator, _release, _vtable_method

logger = getLogger("audio_sources")

_S_OK = 0
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106
_COINIT_MULTITHREADED = 0
_CLSCTX_ALL = 23

_IID_IAudioSessionManager2 = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
_IID_IAudioSessionControl2 = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")

#: A session that has played something recently, as opposed to one merely open.
_STATE_ACTIVE = 1

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002

#: A tree deeper than this is not a browser, it is a loop.
_MAX_TREE_DEPTH = 8

_ole32 = ctypes.windll.ole32
_kernel32 = ctypes.windll.kernel32


class _ProcessEntry32W(ctypes.Structure):
	_fields_ = [
		("dwSize", DWORD),
		("cntUsage", DWORD),
		("th32ProcessID", DWORD),
		("th32DefaultHeapID", POINTER(ctypes.c_ulong)),
		("th32ModuleID", DWORD),
		("cntThreads", DWORD),
		("th32ParentProcessID", DWORD),
		("pcPriClassBase", LONG),
		("dwFlags", DWORD),
		("szExeFile", WCHAR * MAX_PATH),
	]


def process_name(pid):
	"""Return the file name of the program a process runs, or None."""
	if not pid:
		return None
	handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, DWORD(pid))
	if not handle:
		return None
	try:
		size = DWORD(MAX_PATH)
		buffer = ctypes.create_unicode_buffer(MAX_PATH)
		query = _kernel32.QueryFullProcessImageNameW
		query.argtypes = [HANDLE, DWORD, ctypes.c_wchar_p, POINTER(DWORD)]
		query.restype = BOOL
		if not query(handle, 0, buffer, byref(size)):
			return None
		return os.path.basename(buffer.value).lower() or None
	finally:
		_kernel32.CloseHandle(handle)


def _parents():
	"""Return, for every process, its parent and its executable name."""
	snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
	if snapshot == -1 or not snapshot:
		return {}
	tree = {}
	try:
		entry = _ProcessEntry32W()
		entry.dwSize = sizeof(_ProcessEntry32W)
		more = _kernel32.Process32FirstW(snapshot, byref(entry))
		while more:
			tree[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
			more = _kernel32.Process32NextW(snapshot, byref(entry))
	finally:
		_kernel32.CloseHandle(snapshot)
	return tree


def root_of(pid, tree=None):
	"""Return the process at the top of the run of same named processes above this one.

	A browser plays its sound from a helper process it started, which carries the very
	same program name as the window. Climbing while the name stays the same therefore
	lands on the window the user would point at, and stops before reaching whatever
	started the browser itself.
	"""
	if tree is None:
		tree = _parents()
	current = pid
	entry = tree.get(current)
	if entry is None:
		return pid
	name = entry[1]
	for _ in range(_MAX_TREE_DEPTH):
		parent = tree.get(current)
		if parent is None:
			break
		parent_pid = parent[0]
		above = tree.get(parent_pid)
		if above is None or above[1] != name or parent_pid == current:
			break
		current = parent_pid
	return current


def _session_manager():
	enumerator = c_void_p()
	if _ole32.CoCreateInstance(
		byref(_CLSID_MMDeviceEnumerator), None, _CLSCTX_ALL,
		byref(_IID_IMMDeviceEnumerator), byref(enumerator),
	) != _S_OK:
		return None
	device = c_void_p()
	try:
		# GetDefaultAudioEndpoint(eRender, eConsole, &device)
		if _vtable_method(enumerator, 4, ctypes.c_int, ctypes.c_int, POINTER(c_void_p))(
			enumerator, 0, 0, byref(device)
		) != _S_OK:
			return None
	finally:
		_release(enumerator)
	manager = c_void_p()
	try:
		if _vtable_method(device, 3, POINTER(GUID), DWORD, c_void_p, POINTER(c_void_p))(
			device, byref(_IID_IAudioSessionManager2), _CLSCTX_ALL, None, byref(manager)
		) != _S_OK:
			return None
	finally:
		_release(device)
	return manager


def list_sessions():
	"""Return the programs holding an audio session, most recently heard first.

	Each entry is a dictionary with the name of the executable, the process at the
	root of its tree, and whether it is playing something at this instant. The list
	never mentions the process this code runs in.
	"""
	# This is called from a thread of its own, which has to be joined to the component
	# model before anything can be asked of it. A thread already joined as a single
	# threaded apartment answers that the mode differs, and is left alone.
	result = _ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
	uninitialise = result != _RPC_E_CHANGED_MODE
	try:
		return _list_sessions()
	finally:
		if uninitialise:
			_ole32.CoUninitialize()


def _list_sessions():
	manager = _session_manager()
	if not manager:
		return []
	sessions = []
	own = os.getpid()
	tree = _parents()
	enumerator = c_void_p()
	try:
		if _vtable_method(manager, 5, POINTER(c_void_p))(manager, byref(enumerator)) != _S_OK:
			return []
	finally:
		_release(manager)
	try:
		count = ctypes.c_int()
		if _vtable_method(enumerator, 3, POINTER(ctypes.c_int))(enumerator, byref(count)) != _S_OK:
			return []
		seen = {}
		for index in range(count.value):
			control = c_void_p()
			if _vtable_method(enumerator, 4, ctypes.c_int, POINTER(c_void_p))(
				enumerator, index, byref(control)
			) != _S_OK:
				continue
			try:
				entry = _read_session(control, own, tree)
			finally:
				_release(control)
			if entry is None:
				continue
			previous = seen.get(entry["name"])
			if previous is None:
				seen[entry["name"]] = entry
				sessions.append(entry)
			elif entry["active"]:
				previous["active"] = True
	finally:
		_release(enumerator)
	sessions.sort(key=lambda item: (not item["active"], item["name"]))
	return sessions


def _read_session(control, own, tree):
	extended = c_void_p()
	if _vtable_method(control, 0, POINTER(GUID), POINTER(c_void_p))(
		control, byref(_IID_IAudioSessionControl2), byref(extended)
	) != _S_OK:
		return None
	try:
		# IAudioSessionControl2::IsSystemSoundsSession returns S_OK for the session
		# Windows itself plays its notifications through, which belongs to no program.
		if _vtable_method(extended, 15)(extended) == _S_OK:
			return None
		pid = DWORD()
		if _vtable_method(extended, 14, POINTER(DWORD))(extended, byref(pid)) != _S_OK:
			return None
		state = ctypes.c_int()
		if _vtable_method(extended, 3, POINTER(ctypes.c_int))(extended, byref(state)) != _S_OK:
			state.value = 0
	finally:
		_release(extended)
	if not pid.value or pid.value == own:
		return None
	root = root_of(pid.value, tree)
	if root == own:
		return None
	name = process_name(root) or process_name(pid.value)
	if not name:
		return None
	return {"name": name, "pid": root, "active": state.value == _STATE_ACTIVE}


def resolve(name, tree=None):
	"""Return the root processes currently running the named executable."""
	if tree is None:
		tree = _parents()
	wanted = name.lower()
	roots = set()
	for pid, (_parent, exe) in tree.items():
		if exe == wanted:
			roots.add(root_of(pid, tree))
	return sorted(roots)
