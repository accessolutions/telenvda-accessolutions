"""Capture of the sound produced by chosen applications, on Windows.

Windows offers no way to capture "everything except this program". What it offers,
since Windows 10 version 2004, is a loopback capture bound to one process tree:
either that tree alone, or everything but that tree. Only one tree can be named per
capture, so a session that must carry three applications and leave NVDA out is built
the only way the platform allows, by opening one capture per wanted application and
mixing the results here. NVDA is then simply never in the list, which is a stronger
guarantee than asking the system to leave it out.

On Windows releases too old for that interface, the only capture available is the
one of the whole sound card. It is offered as a fallback, but the caller must warn
the user first: the voice of the remote NVDA is in it, and the person listening will
hear it twice.

Nothing here is specific to the remote sound feature. A capture produces sixteen bit
stereo frames at forty-eight kilohertz, and what leaves this module is the sum of
those captures reduced to a single channel at sixteen kilohertz, which is what a
person needs to recognise what a computer is doing and costs six times less to send.

The interfaces are called through raw vtable pointers rather than a COM library. The
completion handler that :func:`ActivateAudioInterfaceAsync` demands has to be an
object Windows can call back into, so a small vtable is built by hand for it.
"""

import ctypes
import os
import struct
import sys
import threading
import time
from ctypes import POINTER, byref, c_void_p, sizeof
from ctypes.wintypes import BYTE, DWORD, HANDLE, LPCWSTR, UINT, WORD

# NVDA only adds its handlers to its own logger, so a logger of this module's own
# would write nothing at all below the warning level.
from logHandler import log as logger


#: Format every capture is opened with, so that mixing is a plain sum of samples.
SAMPLE_RATE = 48000
CHANNELS = 2
BITS_PER_SAMPLE = 16
BYTES_PER_FRAME = CHANNELS * BITS_PER_SAMPLE // 8

#: Length of the blocks handed to the callback. Twenty milliseconds is short enough
#: that the delay it adds cannot be noticed, and long enough that the work done once
#: per block does not weigh.
BLOCK_MS = 20
BLOCK_FRAMES = SAMPLE_RATE * BLOCK_MS // 1000
BLOCK_BYTES = BLOCK_FRAMES * BYTES_PER_FRAME

#: Format the mixed sound leaves this module in. What is carried is not music but
#: what a computer is doing, and one channel at sixteen kilohertz says that just as
#: well as two at forty-eight while costing a sixth as much to send. It is also six
#: times less to add up, which is what keeps the mixing off the back of the speech.
OUTPUT_RATE = 16000
OUTPUT_CHANNELS = 1
OUTPUT_BLOCK_FRAMES = OUTPUT_RATE * BLOCK_MS // 1000
OUTPUT_BLOCK_BYTES = OUTPUT_BLOCK_FRAMES * 2

#: Whole number of captured frames that make one output frame.
_DECIMATION = SAMPLE_RATE // OUTPUT_RATE

#: A block of silence, sent whenever a source has nothing to offer.
SILENCE = b"\0" * OUTPUT_BLOCK_BYTES

#: Windows 10 version 2004, the first release able to capture one process.
_MIN_BUILD_FOR_PROCESS_CAPTURE = 19041

#: Take everything the named process tree produces, or everything but it.
MODE_INCLUDE = 0
MODE_EXCLUDE = 1

_S_OK = 0
_E_NOINTERFACE = -2147467262  # 0x80004002
_E_POINTER = -2147467261  # 0x80004003
_AUDCLNT_S_NO_SINGLE_PROCESS = 0x0008890D

_CLSCTX_ALL = 23
_COINIT_MULTITHREADED = 0

_AUDCLNT_SHAREMODE_SHARED = 0
_AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
_AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
_AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
_AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
_AUDCLNT_BUFFERFLAGS_SILENT = 0x2

_WAVE_FORMAT_PCM = 1
_WAVE_FORMAT_IEEE_FLOAT = 3
_WAVE_FORMAT_EXTENSIBLE = 0xFFFE

_VT_BLOB = 65

#: Buffer asked of the engine, in hundreds of nanoseconds. Larger than one block so
#: that a moment of scheduling pressure does not cost any sound.
_BUFFER_DURATION = 200000  # 20 ms

#: Longest a capture thread waits for a block before checking whether it should stop.
_WAIT_MS = 200

#: The virtual device that stands for the sound of a process tree.
_PROCESS_LOOPBACK_DEVICE = "VAD\\Process_Loopback"

_ole32 = ctypes.windll.ole32
_kernel32 = ctypes.windll.kernel32


class GUID(ctypes.Structure):
	_fields_ = [
		("Data1", DWORD),
		("Data2", WORD),
		("Data3", WORD),
		("Data4", BYTE * 8),
	]

	def __init__(self, text=None):
		super().__init__()
		if text is not None:
			_ole32.CLSIDFromString(LPCWSTR(text), byref(self))

	def __eq__(self, other):
		return isinstance(other, GUID) and bytes(self) == bytes(other)

	def __hash__(self):
		return hash(bytes(self))


_IID_IUnknown = GUID("{00000000-0000-0000-C000-000000000046}")
_IID_IAgileObject = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
_IID_IActivateAudioInterfaceCompletionHandler = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
_IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
_IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
_CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")


class WAVEFORMATEX(ctypes.Structure):
	_fields_ = [
		("wFormatTag", WORD),
		("nChannels", WORD),
		("nSamplesPerSec", DWORD),
		("nAvgBytesPerSec", DWORD),
		("nBlockAlign", WORD),
		("wBitsPerSample", WORD),
		("cbSize", WORD),
	]


class _ProcessLoopbackParams(ctypes.Structure):
	_fields_ = [
		("TargetProcessId", DWORD),
		("ProcessLoopbackMode", ctypes.c_int),
	]


class _ActivationParams(ctypes.Structure):
	_fields_ = [
		("ActivationType", ctypes.c_int),
		("ProcessLoopbackParams", _ProcessLoopbackParams),
	]


class _Blob(ctypes.Structure):
	_fields_ = [("cbSize", ctypes.c_ulong), ("pBlobData", POINTER(BYTE))]


class _PropVariant(ctypes.Structure):
	"""Only the shape needed to pass a blob. The rest of the union is padding."""

	_fields_ = [
		("vt", WORD),
		("wReserved1", WORD),
		("wReserved2", WORD),
		("wReserved3", WORD),
		("blob", _Blob),
		("padding", BYTE * 8),
	]


def _capture_format():
	fmt = WAVEFORMATEX()
	fmt.wFormatTag = _WAVE_FORMAT_PCM
	fmt.nChannels = CHANNELS
	fmt.nSamplesPerSec = SAMPLE_RATE
	fmt.wBitsPerSample = BITS_PER_SAMPLE
	fmt.nBlockAlign = BYTES_PER_FRAME
	fmt.nAvgBytesPerSec = SAMPLE_RATE * BYTES_PER_FRAME
	fmt.cbSize = 0
	return fmt


def _vtable_method(pointer, index, *argtypes):
	"""Return the method at the given slot of the object's vtable, ready to call."""
	table = ctypes.cast(pointer, POINTER(POINTER(c_void_p))).contents
	prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)
	return prototype(table[index])


def _release(pointer):
	if pointer:
		try:
			_vtable_method(pointer, 2)(pointer)
		except Exception:
			logger.exception("Unable to release an audio interface")


def _check(result, what):
	if result != _S_OK:
		raise OSError("%s failed with 0x%08X" % (what, result & 0xFFFFFFFF))


def is_process_capture_available():
	"""Whether this Windows can capture the sound of a single process tree."""
	if sys.platform != "win32":
		return False
	if getattr(sys, "getwindowsversion", None) is None:
		return False
	if sys.getwindowsversion().build < _MIN_BUILD_FOR_PROCESS_CAPTURE:
		return False
	return hasattr(ctypes.windll.mmdevapi, "ActivateAudioInterfaceAsync")


class _CompletionHandler:
	"""The object Windows calls back once an audio interface has been activated.

	:func:`ActivateAudioInterfaceAsync` answers through an interface rather than a
	return value, so a small COM object is assembled here: a vtable of four function
	pointers and a structure whose first field points at it. It also claims to be an
	agile object, which spares Windows from marshalling the call across apartments.
	"""

	_QueryInterface = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, c_void_p, POINTER(c_void_p))
	_AddRef = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)
	_Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)
	_ActivateCompleted = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, c_void_p)

	class _Vtable(ctypes.Structure):
		pass

	class _Object(ctypes.Structure):
		pass

	def __init__(self):
		self.done = threading.Event()
		self._methods = (
			self._QueryInterface(self._query_interface),
			self._AddRef(lambda this: 1),
			self._Release(lambda this: 1),
			self._ActivateCompleted(self._activate_completed),
		)
		self._vtable = self._Vtable(*self._methods)
		self._object = self._Object(ctypes.pointer(self._vtable))
		self.pointer = ctypes.cast(ctypes.pointer(self._object), c_void_p)

	def _query_interface(self, this, riid, out):
		if not out:
			return _E_POINTER
		wanted = ctypes.cast(riid, POINTER(GUID)).contents
		if wanted in (_IID_IUnknown, _IID_IActivateAudioInterfaceCompletionHandler, _IID_IAgileObject):
			out[0] = this
			return _S_OK
		out[0] = None
		return _E_NOINTERFACE

	def _activate_completed(self, this, operation):
		self.done.set()
		return _S_OK


_CompletionHandler._Vtable._fields_ = [
	("QueryInterface", _CompletionHandler._QueryInterface),
	("AddRef", _CompletionHandler._AddRef),
	("Release", _CompletionHandler._Release),
	("ActivateCompleted", _CompletionHandler._ActivateCompleted),
]
_CompletionHandler._Object._fields_ = [("lpVtbl", POINTER(_CompletionHandler._Vtable))]


def _activate_process_loopback(pid, mode):
	"""Return an IAudioClient bound to the sound of one process tree.

	The call is asynchronous even though nothing here has anything to do while it
	runs, so it is simply waited on.
	"""
	params = _ActivationParams()
	params.ActivationType = 1  # Process loopback.
	params.ProcessLoopbackParams.TargetProcessId = pid
	params.ProcessLoopbackParams.ProcessLoopbackMode = mode

	variant = _PropVariant()
	variant.vt = _VT_BLOB
	variant.blob.cbSize = sizeof(params)
	variant.blob.pBlobData = ctypes.cast(ctypes.pointer(params), POINTER(BYTE))

	handler = _CompletionHandler()
	operation = c_void_p()
	activate = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
	activate.restype = ctypes.HRESULT
	activate.argtypes = [LPCWSTR, POINTER(GUID), POINTER(_PropVariant), c_void_p, POINTER(c_void_p)]
	_check(
		activate(
			_PROCESS_LOOPBACK_DEVICE,
			byref(_IID_IAudioClient),
			byref(variant),
			handler.pointer,
			byref(operation),
		),
		"ActivateAudioInterfaceAsync",
	)
	try:
		if not handler.done.wait(5.0):
			raise OSError("The audio engine never answered the activation request")
		result = ctypes.HRESULT()
		client = c_void_p()
		_check(
			_vtable_method(operation, 3, POINTER(ctypes.HRESULT), POINTER(c_void_p))(
				operation, byref(result), byref(client)
			),
			"GetActivateResult",
		)
		_check(result.value or _S_OK, "Activation of the process loopback")
		if not client:
			raise OSError("The audio engine returned no capture interface")
		return client
	finally:
		_release(operation)


def _activate_system_loopback():
	"""Return an IAudioClient bound to everything the sound card plays."""
	enumerator = c_void_p()
	_check(
		_ole32.CoCreateInstance(
			byref(_CLSID_MMDeviceEnumerator),
			None,
			_CLSCTX_ALL,
			byref(_IID_IMMDeviceEnumerator),
			byref(enumerator),
		),
		"CoCreateInstance(MMDeviceEnumerator)",
	)
	device = c_void_p()
	try:
		# GetDefaultAudioEndpoint(eRender, eConsole, &device)
		_check(
			_vtable_method(enumerator, 4, ctypes.c_int, ctypes.c_int, POINTER(c_void_p))(
				enumerator, 0, 0, byref(device)
			),
			"GetDefaultAudioEndpoint",
		)
	finally:
		_release(enumerator)
	client = c_void_p()
	try:
		# IMMDevice::Activate(riid, clsctx, params, &interface)
		_check(
			_vtable_method(device, 3, POINTER(GUID), DWORD, c_void_p, POINTER(c_void_p))(
				device, byref(_IID_IAudioClient), _CLSCTX_ALL, None, byref(client)
			),
			"IMMDevice::Activate",
		)
	finally:
		_release(device)
	return client


class _Capture:
	"""One loopback capture, pumping its blocks into a callback from its own thread."""

	def __init__(self, on_block, describe):
		self._on_block = on_block
		self._describe = describe
		self._stop = threading.Event()
		self._thread = None
		self.failure = None

	@property
	def running(self):
		thread = self._thread
		return thread is not None and thread.is_alive()

	def start(self):
		if self.running:
			return
		self._stop.clear()
		self._thread = threading.Thread(target=self._run, name="audio_capture", daemon=True)
		self._thread.start()

	def stop(self):
		self._stop.set()
		thread, self._thread = self._thread, None
		if thread is not None and thread is not threading.current_thread():
			thread.join(timeout=2.0)

	def _open(self):
		"""Return the IAudioClient to capture from. Implemented by the subclasses."""
		raise NotImplementedError

	def _stream_flags(self):
		return _AUDCLNT_STREAMFLAGS_LOOPBACK | _AUDCLNT_STREAMFLAGS_EVENTCALLBACK

	def _run(self):
		_ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
		client = None
		capture = None
		event = None
		try:
			client = self._open()
			fmt = _capture_format()
			flags = self._stream_flags()
			_check(
				_vtable_method(
					client, 3, ctypes.c_int, DWORD, ctypes.c_longlong, ctypes.c_longlong,
					POINTER(WAVEFORMATEX), POINTER(GUID),
				)(client, _AUDCLNT_SHAREMODE_SHARED, flags, _BUFFER_DURATION, 0, byref(fmt), None),
				"IAudioClient::Initialize",
			)
			event = _kernel32.CreateEventW(None, False, False, None)
			if not event:
				raise OSError("Unable to create the audio capture event")
			_check(
				_vtable_method(client, 13, HANDLE)(client, event),
				"IAudioClient::SetEventHandle",
			)
			capture = c_void_p()
			_check(
				_vtable_method(client, 14, POINTER(GUID), POINTER(c_void_p))(
					client, byref(_IID_IAudioCaptureClient), byref(capture)
				),
				"IAudioClient::GetService",
			)
			_check(_vtable_method(client, 10)(client), "IAudioClient::Start")
			try:
				self._pump(capture, event)
			finally:
				_vtable_method(client, 11)(client)  # Stop.
		except Exception as error:
			self.failure = error
			logger.warning("The capture of %s stopped: %s", self._describe, error)
		finally:
			_release(capture)
			_release(client)
			if event:
				_kernel32.CloseHandle(event)
			_ole32.CoUninitialize()

	def _pump(self, capture, event):
		get_buffer = _vtable_method(
			capture, 3,
			POINTER(POINTER(BYTE)), POINTER(UINT), POINTER(DWORD),
			POINTER(ctypes.c_ulonglong), POINTER(ctypes.c_ulonglong),
		)
		release_buffer = _vtable_method(capture, 4, UINT)
		next_packet = _vtable_method(capture, 5, POINTER(UINT))
		data = POINTER(BYTE)()
		frames = UINT()
		flags = DWORD()
		available = UINT()
		while not self._stop.is_set():
			_kernel32.WaitForSingleObject(event, _WAIT_MS)
			while not self._stop.is_set():
				result = next_packet(capture, byref(available))
				if result != _S_OK or not available.value:
					break
				result = get_buffer(capture, byref(data), byref(frames), byref(flags), None, None)
				if result != _S_OK and result != _AUDCLNT_S_NO_SINGLE_PROCESS:
					return
				count = frames.value
				if count:
					if flags.value & _AUDCLNT_BUFFERFLAGS_SILENT:
						block = b"\0" * (count * BYTES_PER_FRAME)
					else:
						block = ctypes.string_at(data, count * BYTES_PER_FRAME)
					try:
						self._on_block(block)
					except Exception:
						logger.exception("Error while handling a captured audio block")
				release_buffer(capture, count)


class ProcessCapture(_Capture):
	"""The sound of one process and of the processes it started.

	A browser plays its sound from a child process rather than from the window the
	user sees, so the whole tree is taken and the caller is expected to name the
	process at the root of it.
	"""

	def __init__(self, pid, on_block, mode=MODE_INCLUDE):
		super().__init__(on_block, "process %d" % pid)
		self.pid = pid
		self.mode = mode

	def _open(self):
		return _activate_process_loopback(self.pid, self.mode)


class SystemCapture(_Capture):
	"""Everything the sound card plays, including the voice of the local NVDA."""

	def __init__(self, on_block):
		super().__init__(on_block, "the sound card")

	def _open(self):
		return _activate_system_loopback()

	def _stream_flags(self):
		# The sound card imposes its own mixing format, so the engine is asked to
		# convert to the one every capture here speaks.
		return (
			super()._stream_flags()
			| _AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
			| _AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
		)


class _Source:
	"""One capture and the sound it has produced but that was not mixed in yet.

	What is kept here is already reduced to the output format, so that the reduction
	is done once per capture rather than once per capture and again on the sum, and
	so that the mixing itself has six times fewer samples to add up.
	"""

	#: Beyond this, the reader is too slow and the oldest sound is dropped rather than
	#: kept, because sound that arrives late is worse than sound that never arrives.
	MAX_PENDING = OUTPUT_BLOCK_BYTES * 25  # Half a second.

	def __init__(self, capture):
		self.capture = capture
		self.buffer = bytearray()
		self.lock = threading.Lock()
		#: Frames of the previous block that did not fill a whole output frame.
		self._rest = b""

	def feed(self, block):
		if self._rest:
			block = self._rest + block
		usable = len(block) - (len(block) % (BYTES_PER_FRAME * _DECIMATION))
		self._rest = block[usable:]
		if not usable:
			return
		reduced = downmix(block[:usable])
		with self.lock:
			self.buffer += reduced
			excess = len(self.buffer) - self.MAX_PENDING
			if excess > 0:
				del self.buffer[:excess]

	def take(self, size):
		with self.lock:
			if not self.buffer:
				return None
			block = bytes(self.buffer[:size])
			del self.buffer[:size]
		if len(block) < size:
			block += b"\0" * (size - len(block))
		return block


class Mixer:
	"""Several captures, summed into one steady stream of blocks.

	The captures do not tick together, and a silent application produces nothing at
	all rather than silence, so the blocks are not simply chained as they arrive.
	A clock ticks every twenty milliseconds instead, takes whatever each source has
	to offer, treats what is missing as silence, and sums.
	"""

	def __init__(self, on_block):
		self._on_block = on_block
		self._sources = {}
		self._lock = threading.Lock()
		self._stop = threading.Event()
		self._thread = None

	@property
	def keys(self):
		with self._lock:
			return set(self._sources)

	def add(self, key, capture_factory):
		"""Start capturing one more source, unless that key is already captured."""
		with self._lock:
			if key in self._sources:
				return False
		source = _Source(None)
		source.capture = capture_factory(source.feed)
		with self._lock:
			if key in self._sources:
				return False
			self._sources[key] = source
		source.capture.start()
		return True

	def remove(self, key):
		with self._lock:
			source = self._sources.pop(key, None)
		if source is None:
			return False
		source.capture.stop()
		return True

	def start(self):
		if self._thread is not None:
			return
		self._stop.clear()
		self._thread = threading.Thread(target=self._run, name="audio_mixer", daemon=True)
		self._thread.start()

	def stop(self):
		self._stop.set()
		thread, self._thread = self._thread, None
		if thread is not None and thread is not threading.current_thread():
			thread.join(timeout=2.0)
		with self._lock:
			sources, self._sources = self._sources, {}
		for source in sources.values():
			source.capture.stop()

	def _run(self):
		# The clock is followed rather than the elapsed time of each round, so that a
		# slow round is caught up with instead of shifting every block after it.
		next_tick = time.monotonic()
		while not self._stop.is_set():
			next_tick += BLOCK_MS / 1000.0
			delay = next_tick - time.monotonic()
			if delay > 0:
				if self._stop.wait(delay):
					return
			elif delay < -1.0:
				next_tick = time.monotonic()
			try:
				self._on_block(self._mix())
			except Exception:
				logger.exception("Error while handing over a mixed audio block")

	def _mix(self):
		with self._lock:
			sources = list(self._sources.values())
		blocks = [block for block in (source.take(OUTPUT_BLOCK_BYTES) for source in sources) if block]
		if not blocks:
			return SILENCE
		if len(blocks) == 1:
			return blocks[0]
		unpack = struct.Struct("<%dh" % OUTPUT_BLOCK_FRAMES).unpack
		total = [0] * OUTPUT_BLOCK_FRAMES
		for block in blocks:
			for index, sample in enumerate(unpack(block)):
				total[index] += sample
		# Two applications playing at once can sum past what a sixteen bit sample can
		# hold. Clipping there is what every mixer does, and it is inaudible unless
		# both were already close to the maximum.
		for index, sample in enumerate(total):
			if sample > 32767:
				total[index] = 32767
			elif sample < -32768:
				total[index] = -32768
		return struct.Struct("<%dh" % OUTPUT_BLOCK_FRAMES).pack(*total)


def own_process_tree_root():
	"""Return the identifier of the process NVDA runs in.

	It is never captured, and it is named here rather than compared by file name so
	that no naming accident can let the voice of the screen reader through.
	"""
	return os.getpid()


# The reduction to the format the sound is carried in, and the compression applied
# to it. None of this uses audioop, which the standard library dropped after Python
# 3.12 and which NVDA will therefore stop shipping one day.


def downmix(block):
	"""Reduce captured frames to one channel at the output rate.

	The two channels are averaged and one frame in three is kept. Dropping frames
	without filtering what lies above the new rate does fold the highest frequencies
	back into the audible range, but what is carried here is the sound of a computer
	working, not music, and nothing of what makes it recognisable lives up there.
	"""
	frames = len(block) // BYTES_PER_FRAME
	samples = struct.Struct("<%dh" % (frames * CHANNELS)).unpack(block)
	kept = range(0, frames * CHANNELS, CHANNELS * _DECIMATION)
	return struct.Struct("<%dh" % len(kept)).pack(
		*[(samples[index] + samples[index + 1]) >> 1 for index in kept]
	)


#: Sixteen bit samples are folded into eight before being sent. This is the G.711
#: mu-law of the telephone: it keeps the quiet parts and coarsens the loud ones,
#: which is exactly what the ear does, and halves the size for a loss that is barely
#: heard on speech. Nothing better can be done without an encoder written in C.
#: The arithmetic below is the reference one, which works on the top fourteen bits.
_MULAW_BIAS = 0x84
_MULAW_CLIP = 8159
_MULAW_BOUNDS = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)

_mulaw_encode_table = None


def _build_mulaw_encode_table():
	table = bytearray(65536)
	for index in range(65536):
		sample = (index - 65536 if index >= 32768 else index) >> 2
		if sample < 0:
			sample = -sample
			mask = 0x7F
		else:
			mask = 0xFF
		if sample > _MULAW_CLIP:
			sample = _MULAW_CLIP
		sample += _MULAW_BIAS >> 2
		for segment, bound in enumerate(_MULAW_BOUNDS):
			if sample <= bound:
				table[index] = ((segment << 4) | ((sample >> (segment + 1)) & 0x0F)) ^ mask
				break
		else:
			table[index] = 0x7F ^ mask
	return bytes(table)


def _build_mulaw_decode_table():
	table = []
	for encoded in range(256):
		value = ~encoded & 0xFF
		sample = (((value & 0x0F) << 3) + _MULAW_BIAS) << ((value & 0x70) >> 4)
		table.append(_MULAW_BIAS - sample if value & 0x80 else sample - _MULAW_BIAS)
	return tuple(table)


_MULAW_DECODE = _build_mulaw_decode_table()


def compress(block):
	"""Fold sixteen bit samples into eight, halving what has to be sent."""
	global _mulaw_encode_table
	if _mulaw_encode_table is None:
		# Sixty-five thousand entries, built once and only if the sound is ever used.
		_mulaw_encode_table = _build_mulaw_encode_table()
	table = _mulaw_encode_table
	samples = struct.Struct("<%dh" % (len(block) // 2)).unpack(block)
	return bytes([table[sample & 0xFFFF] for sample in samples])


def decode_table(volume=1.0):
	"""Return a decoding table that also applies a volume, for free.

	Turning the volume down means multiplying every sample, which in Python would
	cost more than everything else here put together. Since a compressed sample can
	only take two hundred and fifty-six values, the multiplication is done once per
	value instead of once per sample.
	"""
	if volume >= 1.0:
		return _MULAW_DECODE
	return tuple(max(-32768, min(32767, int(sample * volume))) for sample in _MULAW_DECODE)


def expand(block, table=None):
	"""Turn compressed samples back into the sixteen bit frames a player expects."""
	if table is None:
		table = _MULAW_DECODE
	return struct.Struct("<%dh" % len(block)).pack(*[table[byte] for byte in block])
