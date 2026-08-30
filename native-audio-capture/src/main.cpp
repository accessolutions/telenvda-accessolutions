// nvda_audio_capture - capture the Windows audio mix with one process tree excluded,
// and hand it to the browser page over a loopback WebSocket.
//
// Windows 10 build 20348 and later expose a "process loopback" activation of
// IAudioClient. Given a process id it can capture either only what that process
// tree renders, or everything except what it renders. The second mode is what we
// need: the whole system mix minus NVDA, so the sound of the controlled computer
// can be streamed without carrying NVDA's own speech along with it.
//
// A web page cannot reach this API, which is why the browser capture used today
// unavoidably contains the remote NVDA. This program does the capture natively and
// serves the samples to the page, which turns them back into a WebRTC track.
//
// Two modes:
//   measure  (default) report the level once per second, optionally write a WAV.
//            Everything can be checked on a single machine this way.
//   --serve  bind a WebSocket on 127.0.0.1, print the chosen port, wait for the
//            page and stream 48 kHz stereo 16 bit PCM to it.
//
// The serving mode repeats, deliberately, the four protections of local_bridge.py:
// the socket binds to 127.0.0.1 alone, the port is chosen by the system for each
// session, every connection must carry the session token, compared in constant
// time, and an Origin header that is not the exact expected one is refused. The
// token arrives on standard input rather than on the command line, which other
// processes on the machine can read.

#include <winsock2.h>
#include <ws2tcpip.h>

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <tlhelp32.h>
#include <objbase.h>
#include <bcrypt.h>
#include <wincrypt.h>

#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

// ---------------------------------------------------------------------------
// Declarations missing from the MinGW headers (Windows SDK 10.0.20348+).
// The API itself lives in mmdevapi.dll and ships with Windows; only the header
// describing these structures is absent, so we restate it here.
// ---------------------------------------------------------------------------

#ifndef VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK
#define VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK L"VAD\\Process_Loopback"
#endif

enum ActivationType {
	ACTIVATION_TYPE_DEFAULT = 0,
	ACTIVATION_TYPE_PROCESS_LOOPBACK = 1,
};

enum LoopbackMode {
	LOOPBACK_INCLUDE_TARGET_PROCESS_TREE = 0,
	LOOPBACK_EXCLUDE_TARGET_PROCESS_TREE = 1,
};

struct ProcessLoopbackParams {
	DWORD TargetProcessId;
	LoopbackMode ProcessLoopbackMode;
};

struct ActivationParams {
	ActivationType Type;
	union {
		ProcessLoopbackParams ProcessLoopback;
	};
};

typedef HRESULT(STDAPICALLTYPE *PfnActivateAudioInterfaceAsync)(
	LPCWSTR, REFIID, PROPVARIANT *,
	IActivateAudioInterfaceCompletionHandler *,
	IActivateAudioInterfaceAsyncOperation **);

static const GUID kIID_IAudioClient =
	{0x1CB9AD4C, 0xDBFA, 0x4C32, {0xB1, 0x78, 0xC2, 0xF5, 0x68, 0xA7, 0x03, 0xB2}};
static const GUID kIID_IAudioCaptureClient =
	{0xC8ADBD64, 0xE71E, 0x48A0, {0xA4, 0xDE, 0x18, 0x5C, 0x39, 0x5C, 0xD3, 0x17}};
static const GUID kIID_ICompletionHandler =
	{0x41D949AB, 0x9862, 0x444A, {0x80, 0xF6, 0xC2, 0x61, 0x33, 0x4D, 0xA5, 0xEB}};
static const GUID kIID_IAgileObject =
	{0x94EA2B94, 0xE9CC, 0x49E0, {0xC0, 0xFF, 0xEE, 0x64, 0xCA, 0x8F, 0x5B, 0x90}};

static const int kRate = 48000;
static const int kChannels = 2;

// ---------------------------------------------------------------------------
// Activation is asynchronous and answers through this handler.
// ---------------------------------------------------------------------------

class Handler final : public IActivateAudioInterfaceCompletionHandler, public IAgileObject {
public:
	HANDLE done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
	HRESULT result = E_FAIL;
	IAudioClient *client = nullptr;

	HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void **out) override {
		if (out == nullptr) return E_POINTER;
		if (IsEqualGUID(riid, IID_IUnknown) || IsEqualGUID(riid, kIID_ICompletionHandler)) {
			*out = static_cast<IActivateAudioInterfaceCompletionHandler *>(this);
		} else if (IsEqualGUID(riid, kIID_IAgileObject)) {
			*out = static_cast<IAgileObject *>(this);
		} else {
			*out = nullptr;
			return E_NOINTERFACE;
		}
		AddRef();
		return S_OK;
	}

	ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&refs_); }

	ULONG STDMETHODCALLTYPE Release() override {
		LONG n = InterlockedDecrement(&refs_);
		if (n == 0) delete this;
		return n;
	}

	HRESULT STDMETHODCALLTYPE ActivateCompleted(IActivateAudioInterfaceAsyncOperation *op) override {
		IUnknown *unknown = nullptr;
		HRESULT activation = S_OK;
		result = op->GetActivateResult(&activation, &unknown);
		if (SUCCEEDED(result)) result = activation;
		if (SUCCEEDED(result) && unknown != nullptr) {
			result = unknown->QueryInterface(kIID_IAudioClient, reinterpret_cast<void **>(&client));
		}
		if (unknown != nullptr) unknown->Release();
		SetEvent(done);
		return S_OK;
	}

private:
	~Handler() {
		if (done != nullptr) CloseHandle(done);
	}
	LONG refs_ = 1;
};

// ---------------------------------------------------------------------------
// Small helpers.
// ---------------------------------------------------------------------------

static DWORD find_process(const wchar_t *name) {
	HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
	if (snap == INVALID_HANDLE_VALUE) return 0;
	PROCESSENTRY32W entry;
	entry.dwSize = sizeof(entry);
	DWORD found = 0;
	if (Process32FirstW(snap, &entry)) {
		do {
			if (_wcsicmp(entry.szExeFile, name) == 0) {
				found = entry.th32ProcessID;
				break;
			}
		} while (Process32NextW(snap, &entry));
	}
	CloseHandle(snap);
	return found;
}

static void write_wav(const char *path, const std::vector<short> &samples) {
	FILE *f = fopen(path, "wb");
	if (f == nullptr) {
		printf("Unable to write %s\n", path);
		return;
	}
	const unsigned data_bytes = static_cast<unsigned>(samples.size() * sizeof(short));
	const unsigned byte_rate = static_cast<unsigned>(kRate * kChannels * 2);
	const unsigned short block_align = static_cast<unsigned short>(kChannels * 2);
	const unsigned riff = 36 + data_bytes;
	const unsigned fmt_size = 16;
	const unsigned short pcm = 1;
	const unsigned short ch = static_cast<unsigned short>(kChannels);
	const unsigned short bits = 16;
	const unsigned rate_u = static_cast<unsigned>(kRate);
	fwrite("RIFF", 1, 4, f);
	fwrite(&riff, 4, 1, f);
	fwrite("WAVEfmt ", 1, 8, f);
	fwrite(&fmt_size, 4, 1, f);
	fwrite(&pcm, 2, 1, f);
	fwrite(&ch, 2, 1, f);
	fwrite(&rate_u, 4, 1, f);
	fwrite(&byte_rate, 4, 1, f);
	fwrite(&block_align, 2, 1, f);
	fwrite(&bits, 2, 1, f);
	fwrite("data", 1, 4, f);
	fwrite(&data_bytes, 4, 1, f);
	fwrite(samples.data(), 1, data_bytes, f);
	fclose(f);
	printf("Wrote %s (%.1f s)\n", path, static_cast<double>(samples.size()) / (kRate * kChannels));
}

static double to_dbfs(double rms) {
	if (rms <= 0.0000001) return -120.0;
	return 20.0 * log10(rms);
}

// ---------------------------------------------------------------------------
// Just enough WebSocket to push binary frames at one local page.
// ---------------------------------------------------------------------------

static bool sha1(const std::string &in, BYTE out[20]) {
	BCRYPT_ALG_HANDLE alg = nullptr;
	if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA1_ALGORITHM, nullptr, 0) != 0) return false;
	BCRYPT_HASH_HANDLE hash = nullptr;
	bool ok = BCryptCreateHash(alg, &hash, nullptr, 0, nullptr, 0, 0) == 0;
	if (ok) ok = BCryptHashData(hash, reinterpret_cast<PUCHAR>(const_cast<char *>(in.data())),
	                            static_cast<ULONG>(in.size()), 0) == 0;
	if (ok) ok = BCryptFinishHash(hash, out, 20, 0) == 0;
	if (hash != nullptr) BCryptDestroyHash(hash);
	BCryptCloseAlgorithmProvider(alg, 0);
	return ok;
}

static std::string base64(const BYTE *data, DWORD len) {
	DWORD chars = 0;
	if (!CryptBinaryToStringA(data, len, CRYPT_STRING_BASE64 | CRYPT_STRING_NOCRLF, nullptr, &chars)) {
		return std::string();
	}
	std::string out(chars, '\0');
	if (!CryptBinaryToStringA(data, len, CRYPT_STRING_BASE64 | CRYPT_STRING_NOCRLF, &out[0], &chars)) {
		return std::string();
	}
	out.resize(strlen(out.c_str()));
	return out;
}

/// Compare without leaking, through timing, how many characters matched.
static bool same_secret(const std::string &a, const std::string &b) {
	if (a.size() != b.size()) return false;
	unsigned char diff = 0;
	for (size_t i = 0; i < a.size(); i++) {
		diff |= static_cast<unsigned char>(a[i] ^ b[i]);
	}
	return diff == 0;
}

static std::string header_value(const std::string &request, const std::string &name) {
	std::string lower;
	lower.reserve(request.size());
	for (char c : request) lower.push_back(static_cast<char>(tolower(static_cast<unsigned char>(c))));
	const std::string needle = "\r\n" + name + ":";
	const size_t at = lower.find(needle);
	if (at == std::string::npos) return std::string();
	size_t start = at + needle.size();
	while (start < request.size() && (request[start] == ' ' || request[start] == '\t')) start++;
	const size_t end = request.find("\r\n", start);
	if (end == std::string::npos) return std::string();
	return request.substr(start, end - start);
}

static std::string query_token(const std::string &request) {
	const size_t line_end = request.find("\r\n");
	if (line_end == std::string::npos) return std::string();
	const std::string line = request.substr(0, line_end);
	const size_t at = line.find("token=");
	if (at == std::string::npos) return std::string();
	size_t end = at + 6;
	while (end < line.size() && line[end] != '&' && line[end] != ' ') end++;
	return line.substr(at + 6, end - (at + 6));
}

static bool send_all(SOCKET s, const char *data, size_t len) {
	size_t sent = 0;
	while (sent < len) {
		const int n = send(s, data + sent, static_cast<int>(len - sent), 0);
		if (n <= 0) return false;
		sent += static_cast<size_t>(n);
	}
	return true;
}

static bool ws_send_binary(SOCKET s, const void *payload, size_t len) {
	char header[10];
	size_t header_len = 0;
	header[0] = static_cast<char>(0x82);  // FIN + binary opcode
	if (len < 126) {
		header[1] = static_cast<char>(len);
		header_len = 2;
	} else if (len <= 0xFFFF) {
		header[1] = 126;
		header[2] = static_cast<char>((len >> 8) & 0xFF);
		header[3] = static_cast<char>(len & 0xFF);
		header_len = 4;
	} else {
		header[1] = 127;
		for (int i = 0; i < 8; i++) {
			header[2 + i] = static_cast<char>((static_cast<unsigned long long>(len) >> ((7 - i) * 8)) & 0xFF);
		}
		header_len = 10;
	}
	if (!send_all(s, header, header_len)) return false;
	return send_all(s, static_cast<const char *>(payload), len);
}

/// Read the request, check the session token and the origin, answer the upgrade.
static bool ws_accept(SOCKET s, const std::string &token, const std::string &origin) {
	std::string request;
	char buf[1024];
	while (request.find("\r\n\r\n") == std::string::npos) {
		if (request.size() > 8192) return false;
		const int n = recv(s, buf, sizeof(buf), 0);
		if (n <= 0) return false;
		request.append(buf, static_cast<size_t>(n));
	}

	if (!same_secret(query_token(request), token)) {
		fprintf(stderr, "Connection refused: invalid token.\n");
		return false;
	}
	const std::string got_origin = header_value(request, "origin");
	if (got_origin != origin) {
		fprintf(stderr, "Connection refused: unexpected origin \"%s\".\n", got_origin.c_str());
		return false;
	}
	const std::string key = header_value(request, "sec-websocket-key");
	if (key.empty()) return false;

	BYTE digest[20];
	if (!sha1(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11", digest)) return false;
	const std::string accept = base64(digest, 20);

	const std::string response =
		"HTTP/1.1 101 Switching Protocols\r\n"
		"Upgrade: websocket\r\n"
		"Connection: Upgrade\r\n"
		"Sec-WebSocket-Accept: " + accept + "\r\n\r\n";
	return send_all(s, response.data(), response.size());
}

// ---------------------------------------------------------------------------
// Capture setup, shared by both modes.
// ---------------------------------------------------------------------------

struct Capture {
	IAudioClient *client = nullptr;
	IAudioCaptureClient *capture = nullptr;
	HANDLE ready = nullptr;
};

static bool start_capture(DWORD pid, bool include, Capture *out) {
	ActivationParams params{};
	params.Type = ACTIVATION_TYPE_PROCESS_LOOPBACK;
	params.ProcessLoopback.TargetProcessId = pid;
	params.ProcessLoopback.ProcessLoopbackMode =
		include ? LOOPBACK_INCLUDE_TARGET_PROCESS_TREE : LOOPBACK_EXCLUDE_TARGET_PROCESS_TREE;

	PROPVARIANT activation{};
	activation.vt = VT_BLOB;
	activation.blob.cbSize = sizeof(params);
	activation.blob.pBlobData = reinterpret_cast<BYTE *>(&params);

	HMODULE dll = LoadLibraryW(L"mmdevapi.dll");
	if (dll == nullptr) {
		fprintf(stderr, "mmdevapi.dll not found.\n");
		return false;
	}
	const auto activate = reinterpret_cast<PfnActivateAudioInterfaceAsync>(
		reinterpret_cast<void *>(GetProcAddress(dll, "ActivateAudioInterfaceAsync")));
	if (activate == nullptr) {
		fprintf(stderr, "ActivateAudioInterfaceAsync is missing: this Windows is too old.\n"
		                "Process loopback needs Windows 10 build 20348 or later.\n");
		return false;
	}

	Handler *handler = new Handler();
	IActivateAudioInterfaceAsyncOperation *op = nullptr;
	HRESULT hr = activate(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, kIID_IAudioClient, &activation, handler, &op);
	if (FAILED(hr)) {
		fprintf(stderr, "ActivateAudioInterfaceAsync failed: 0x%08lX\n", static_cast<unsigned long>(hr));
		return false;
	}
	WaitForSingleObject(handler->done, 5000);
	if (op != nullptr) op->Release();
	if (FAILED(handler->result) || handler->client == nullptr) {
		fprintf(stderr, "Activation refused: 0x%08lX\n", static_cast<unsigned long>(handler->result));
		fprintf(stderr, "If that is 0x88890008, process loopback is not available on this machine.\n");
		return false;
	}
	out->client = handler->client;
	handler->Release();

	// The pseudo device has no mix format to ask for: we impose one.
	WAVEFORMATEX wfx{};
	wfx.wFormatTag = WAVE_FORMAT_PCM;
	wfx.nChannels = static_cast<WORD>(kChannels);
	wfx.nSamplesPerSec = kRate;
	wfx.wBitsPerSample = 16;
	wfx.nBlockAlign = static_cast<WORD>(kChannels * 2);
	wfx.nAvgBytesPerSec = kRate * wfx.nBlockAlign;
	wfx.cbSize = 0;

	hr = out->client->Initialize(AUDCLNT_SHAREMODE_SHARED,
	                             AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
	                             200000 /* 20 ms, en unites de 100 ns */, 0, &wfx, nullptr);
	if (FAILED(hr)) {
		fprintf(stderr, "IAudioClient::Initialize failed: 0x%08lX\n", static_cast<unsigned long>(hr));
		return false;
	}
	out->ready = CreateEventW(nullptr, FALSE, FALSE, nullptr);
	out->client->SetEventHandle(out->ready);
	hr = out->client->GetService(kIID_IAudioCaptureClient, reinterpret_cast<void **>(&out->capture));
	if (FAILED(hr)) {
		fprintf(stderr, "GetService failed: 0x%08lX\n", static_cast<unsigned long>(hr));
		return false;
	}
	return true;
}

// ---------------------------------------------------------------------------

static int run_serve(Capture &cap, const std::string &origin) {
	std::string token;
	{
		char line[512];
		if (fgets(line, sizeof(line), stdin) == nullptr) {
			fprintf(stderr, "No token received on standard input.\n");
			return 1;
		}
		token = line;
		while (!token.empty() && (token.back() == '\n' || token.back() == '\r')) token.pop_back();
	}
	if (token.empty()) {
		fprintf(stderr, "Empty token.\n");
		return 1;
	}

	WSADATA wsa;
	if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
		fprintf(stderr, "WSAStartup failed.\n");
		return 1;
	}
	const SOCKET listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
	if (listener == INVALID_SOCKET) {
		fprintf(stderr, "socket() failed.\n");
		return 1;
	}
	sockaddr_in addr{};
	addr.sin_family = AF_INET;
	addr.sin_port = 0;  // the system picks, so the port cannot be guessed in advance
	InetPtonA(AF_INET, "127.0.0.1", &addr.sin_addr);  // loopback alone, never every interface
	if (bind(listener, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) != 0 || listen(listener, 1) != 0) {
		fprintf(stderr, "bind/listen failed.\n");
		return 1;
	}
	sockaddr_in bound{};
	int bound_len = sizeof(bound);
	getsockname(listener, reinterpret_cast<sockaddr *>(&bound), &bound_len);

	// NVDA reads this line to know where to point the page.
	printf("PORT %d\n", ntohs(bound.sin_port));
	fflush(stdout);

	const SOCKET client = accept(listener, nullptr, nullptr);
	closesocket(listener);
	if (client == INVALID_SOCKET) {
		fprintf(stderr, "accept() failed.\n");
		return 1;
	}
	if (!ws_accept(client, token, origin)) {
		closesocket(client);
		return 1;
	}
	fprintf(stderr, "Page connected, streaming.\n");

	cap.client->Start();
	std::vector<short> silence(static_cast<size_t>(kRate / 100) * kChannels, 0);
	bool alive = true;
	while (alive) {
		WaitForSingleObject(cap.ready, 200);
		for (;;) {
			BYTE *data = nullptr;
			UINT32 frames = 0;
			DWORD flags = 0;
			const HRESULT hr = cap.capture->GetBuffer(&data, &frames, &flags, nullptr, nullptr);
			if (hr == AUDCLNT_S_BUFFER_EMPTY || FAILED(hr) || frames == 0) break;
			const size_t bytes = static_cast<size_t>(frames) * kChannels * 2;
			// Silent packets still have to go out: the page rebuilds a stream from
			// this and its clock must keep being fed, or the track stalls.
			if ((flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0) {
				if (silence.size() * 2 < bytes) silence.assign(bytes / 2, 0);
				alive = ws_send_binary(client, silence.data(), bytes);
			} else {
				alive = ws_send_binary(client, data, bytes);
			}
			cap.capture->ReleaseBuffer(frames);
			if (!alive) break;
		}
	}
	cap.client->Stop();
	closesocket(client);
	fprintf(stderr, "Page disconnected, stopping.\n");
	return 0;
}

static int run_measure(Capture &cap, DWORD pid, bool include, int seconds, const std::string &out) {
	printf(include ? "Capturing ONLY the sound of PID %lu, for %d s.\n"
	               : "Capturing all the system sound EXCEPT that of PID %lu, for %d s.\n",
	       pid, seconds);
	printf("Level per second, in dBFS. -120 means complete silence.\n\n");

	cap.client->Start();

	std::vector<short> recorded;
	const DWORD started = GetTickCount();
	double window_sum = 0.0;
	long window_count = 0;
	double peak = -120.0;
	int printed = 0;

	while (static_cast<int>((GetTickCount() - started) / 1000) < seconds) {
		WaitForSingleObject(cap.ready, 200);
		for (;;) {
			BYTE *data = nullptr;
			UINT32 frames = 0;
			DWORD flags = 0;
			const HRESULT hr = cap.capture->GetBuffer(&data, &frames, &flags, nullptr, nullptr);
			if (hr == AUDCLNT_S_BUFFER_EMPTY || FAILED(hr) || frames == 0) break;
			const short *pcm = reinterpret_cast<const short *>(data);
			const size_t count = static_cast<size_t>(frames) * kChannels;
			if ((flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0) {
				window_count += static_cast<long>(count);
				if (!out.empty()) recorded.insert(recorded.end(), count, 0);
			} else {
				for (size_t i = 0; i < count; i++) {
					const double v = pcm[i] / 32768.0;
					window_sum += v * v;
				}
				window_count += static_cast<long>(count);
				if (!out.empty()) recorded.insert(recorded.end(), pcm, pcm + count);
			}
			cap.capture->ReleaseBuffer(frames);
		}
		const int elapsed = static_cast<int>((GetTickCount() - started) / 1000);
		if (elapsed > printed && window_count > 0) {
			const double db = to_dbfs(sqrt(window_sum / window_count));
			if (db > peak) peak = db;
			printf("  %2d s : %7.1f dBFS\n", elapsed, db);
			fflush(stdout);
			window_sum = 0.0;
			window_count = 0;
			printed = elapsed;
		}
	}

	cap.client->Stop();
	printf("\nLoudest level observed: %.1f dBFS\n", peak);
	if (peak < -90.0) printf("Silence: nothing was captured.\n");
	if (!out.empty()) write_wav(out.c_str(), recorded);
	return 0;
}

// ---------------------------------------------------------------------------

int wmain(int argc, wchar_t **argv) {
	DWORD pid = 0;
	int seconds = 10;
	bool include = false;
	bool serve = false;
	std::string out;
	std::string origin;

	for (int i = 1; i < argc; i++) {
		const std::wstring a = argv[i];
		auto narrow = [&](int index) {
			char buf[512];
			WideCharToMultiByte(CP_UTF8, 0, argv[index], -1, buf, sizeof(buf), nullptr, nullptr);
			return std::string(buf);
		};
		if (a == L"--pid" && i + 1 < argc) {
			pid = static_cast<DWORD>(_wtoi(argv[++i]));
		} else if (a == L"--process" && i + 1 < argc) {
			pid = find_process(argv[++i]);
		} else if (a == L"--seconds" && i + 1 < argc) {
			seconds = _wtoi(argv[++i]);
		} else if (a == L"--include") {
			include = true;
		} else if (a == L"--serve") {
			serve = true;
		} else if (a == L"--origin" && i + 1 < argc) {
			origin = narrow(++i);
		} else if (a == L"--out" && i + 1 < argc) {
			out = narrow(++i);
		} else {
			printf("Usage: nvda_audio_capture [--process nvda.exe | --pid N]\n"
			       "                          [--seconds N] [--include] [--out file.wav]\n"
			       "                          [--serve --origin http://127.0.0.1:PORT]\n"
			       "\n"
			       "By default, captures all the system sound EXCEPT that of the target\n"
			       "process, and reports the level once per second.\n"
			       "--include does the opposite: only that process, which is handy to check\n"
			       "the right one is being targeted.\n"
			       "--serve opens a WebSocket on 127.0.0.1, prints \"PORT n\" on standard\n"
			       "output and streams the PCM. The session token is read from standard\n"
			       "input, and the given origin is the only one accepted.\n");
			return 1;
		}
	}

	if (serve && origin.empty()) {
		fprintf(stderr, "--serve requires --origin.\n");
		return 1;
	}

	if (pid == 0) {
		pid = find_process(L"nvda.exe");
		if (pid == 0) {
			fprintf(stderr, "NVDA not found. Use --pid or --process.\n");
			return 1;
		}
		if (!serve) printf("NVDA found, PID %lu\n", pid);
	}

	const HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
	if (FAILED(hr)) {
		fprintf(stderr, "CoInitializeEx failed: 0x%08lX\n", static_cast<unsigned long>(hr));
		return 1;
	}

	Capture cap;
	if (!start_capture(pid, include, &cap)) return 1;

	const int rc = serve ? run_serve(cap, origin) : run_measure(cap, pid, include, seconds, out);

	if (cap.capture != nullptr) cap.capture->Release();
	if (cap.client != nullptr) cap.client->Release();
	CoUninitialize();
	return rc;
}
