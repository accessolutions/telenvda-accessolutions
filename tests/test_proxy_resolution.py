"""Proxy resolution: candidates, cache, memory and the Windows proxy list parsing."""

import base64
import importlib.util
import socket
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


PACKAGE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient"
PACKAGE_NAME = "proxy_resolution_test_package"


def _load_package():
	package = ModuleType(PACKAGE_NAME)
	package.__path__ = [str(PACKAGE_PATH)]
	sys.modules[PACKAGE_NAME] = package

	configuration = ModuleType(f"{PACKAGE_NAME}.configuration")
	configuration.readonly = False
	configuration.get_config = lambda: _Store()
	sys.modules[configuration.__name__] = configuration

	modules = {}
	for name in ("windows_proxy", "proxy_utils", "proxy_tunnel", "sspi_proxy"):
		spec = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.{name}", PACKAGE_PATH / f"{name}.py")
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		sys.modules[spec.name] = module
		spec.loader.exec_module(module)
		modules[name] = module
	return modules, configuration


class _Store(dict):
	def __init__(self):
		super().__init__(proxy_memory={})
		self.written = 0

	def write(self):
		self.written += 1


modules, configuration = _load_package()
windows_proxy = modules["windows_proxy"]
proxy_utils = modules["proxy_utils"]
sspi_proxy = modules["sspi_proxy"]


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
	store = _Store()
	monkeypatch.setattr(configuration, "get_config", lambda: store)
	proxy_utils.forget()
	proxy_utils._last_decisions.clear()
	monkeypatch.setattr(proxy_utils, "from_system", lambda url: None)
	yield store
	proxy_utils.forget()


@pytest.fixture
def detect(monkeypatch):
	"""Make the Windows detection answer a given result and count the calls."""

	def install(result):
		calls = []

		def detect_proxy(url):
			calls.append(url)
			return result

		monkeypatch.setattr(windows_proxy, "detect_proxy", detect_proxy)
		return calls

	return install


URL = "wss://relay.example:443/"


# --- Windows proxy list parsing -------------------------------------------------


def test_pac_list_keeps_every_candidate_in_order():
	proxy = windows_proxy._parse_proxy_list("proxy-a:8080;proxy-b:8080;proxy-a:8080", URL, "pac")

	assert (proxy.host, proxy.port, proxy.type, proxy.source) == ("proxy-a", 8080, "http", "pac")
	assert [(item.host, item.port) for item in proxy.alternatives] == [("proxy-b", 8080)]


def test_internet_settings_list_prefers_the_scheme_of_the_url():
	proxy = windows_proxy._parse_proxy_list("http=plain:3128;https=secure:3129;socks=socks:1080", URL)

	assert (proxy.host, proxy.port) == ("secure", 3129)
	assert [(item.host, item.port, item.type) for item in proxy.alternatives] == [
		("plain", 3128, "http"),
		("socks", 1080, "socks5"),
	]


def test_machine_settings_are_read_from_the_registry(monkeypatch):
	values = {
		("policy", "ProxySettingsPerUser"): 0,
		("plain", "ProxyEnable"): 1,
		("plain", "ProxyServer"): "corp-proxy:8080",
		("plain", "ProxyOverride"): "<local>",
		("connections", "DefaultConnectionSettings"): bytes([0x46, 0, 0, 0, 1, 0, 0, 0, 0x0B, 0, 0, 0]),
	}

	def registry_value(root, key, name, default=None):
		if key == windows_proxy._INTERNET_SETTINGS_POLICY_KEY:
			kind = "policy"
		elif key.endswith("Connections"):
			kind = "connections"
		else:
			kind = "plain"
		return values.get((kind, name), default)

	monkeypatch.setattr(windows_proxy, "_registry_value", registry_value)
	monkeypatch.setattr(windows_proxy.os, "name", "nt")

	settings = windows_proxy._read_machine_settings()

	assert settings.authoritative is True
	assert settings.proxy == "corp-proxy:8080"
	assert settings.bypass == "<local>"
	assert settings.auto_detect is True
	assert settings.auto_config_url == ""


# --- Resolution: cache, memory, alternatives ------------------------------------


def test_detected_proxy_carries_its_alternatives_and_is_cached(detect):
	calls = detect(
		windows_proxy.DetectedProxy(
			host="proxy-a",
			port=8080,
			type="http",
			source="pac",
			alternatives=(windows_proxy.DetectedProxy(host="proxy-b", port=8080, type="http"),),
		),
	)
	settings = proxy_utils.ProxySettings(auto_detect=True)

	first = proxy_utils.resolve_for_url(settings, URL)
	second = proxy_utils.resolve_for_url(settings, URL)

	assert first == second
	assert len(calls) == 1
	assert first.detected is True
	assert [(item.host, item.port) for item in proxy_utils.candidates(first)] == [
		("proxy-a", 8080),
		("proxy-b", 8080),
	]
	assert proxy_utils.describe(first) == "http://proxy-a:8080 (+1 alternative)"


def test_forgetting_the_url_asks_windows_again(detect):
	calls = detect(windows_proxy.DetectedProxy(host="proxy-a", port=8080, type="http"))
	settings = proxy_utils.ProxySettings(auto_detect=True)

	proxy_utils.resolve_for_url(settings, URL)
	proxy_utils.forget(URL)
	proxy_utils.resolve_for_url(settings, URL)

	assert len(calls) == 2


def test_failed_detection_reuses_the_remembered_proxy(fresh_state, detect):
	fresh_state["proxy_memory"]["relay.example"] = "http://remembered:3128"
	detect(None)

	resolved = proxy_utils.resolve_for_url(proxy_utils.ProxySettings(auto_detect=True), URL)

	assert (resolved.host, resolved.port, resolved.type) == ("remembered", 3128, "http")
	assert proxy_utils.last_decision(URL)[1].startswith("Windows resolution failed; reusing")


def test_remembered_proxy_becomes_a_last_alternative_of_the_detected_one(fresh_state, detect):
	fresh_state["proxy_memory"]["relay.example"] = "http://remembered:3128"
	detect(windows_proxy.DetectedProxy(host="proxy-a", port=8080, type="http"))

	resolved = proxy_utils.resolve_for_url(proxy_utils.ProxySettings(auto_detect=True), URL)

	assert [item.key for item in proxy_utils.candidates(resolved)] == [
		"http://proxy-a:8080",
		"http://remembered:3128",
	]


def test_windows_saying_direct_is_trusted_over_the_memory(fresh_state, detect):
	fresh_state["proxy_memory"]["relay.example"] = "http://remembered:3128"
	detect(windows_proxy.DetectedProxy(source="wpad"))

	resolved = proxy_utils.resolve_for_url(proxy_utils.ProxySettings(auto_detect=True), URL)

	assert resolved.enabled is False


def test_working_detected_proxy_is_remembered_once(fresh_state):
	candidate = proxy_utils.ProxySettings(host="proxy-a", port=8080, type="http", detected=True)

	assert proxy_utils.remember_working_proxy(URL, candidate) is True
	assert proxy_utils.remember_working_proxy(URL, candidate) is False
	assert fresh_state["proxy_memory"]["relay.example"] == "http://proxy-a:8080"
	assert fresh_state.written == 1


def test_manual_proxy_is_not_remembered(fresh_state):
	manual = proxy_utils.ProxySettings(host="proxy-a", port=8080, type="http")

	assert proxy_utils.remember_working_proxy(URL, manual) is False
	assert fresh_state["proxy_memory"] == {}


# --- Opening through candidates --------------------------------------------------


def _settings_with_alternatives():
	return proxy_utils.ProxySettings(
		host="proxy-a",
		port=8080,
		type="http",
		detected=True,
		alternatives=(proxy_utils.ProxySettings(host="proxy-b", port=8080, type="http", detected=True),),
	)


def test_next_candidate_is_tried_when_the_proxy_is_unreachable(fresh_state):
	attempts = []

	def opener(candidate):
		attempts.append(candidate.host)
		if candidate.host == "proxy-a":
			raise ConnectionRefusedError("refused")
		return "socket"

	result, used = proxy_utils.open_through_candidates(_settings_with_alternatives(), URL, opener)

	assert result == "socket"
	assert used.host == "proxy-b"
	assert attempts == ["proxy-a", "proxy-b"]
	assert fresh_state["proxy_memory"]["relay.example"] == "http://proxy-b:8080"


def test_a_working_alternative_is_promoted_in_the_cache(detect):
	detect(
		windows_proxy.DetectedProxy(
			host="proxy-a",
			port=8080,
			type="http",
			alternatives=(windows_proxy.DetectedProxy(host="proxy-b", port=8080, type="http"),),
		),
	)
	settings = proxy_utils.ProxySettings(auto_detect=True)
	resolved = proxy_utils.resolve_for_url(settings, URL)

	proxy_utils.open_through_candidates(
		resolved,
		URL,
		lambda candidate: (_ for _ in ()).throw(socket.timeout()) if candidate.host == "proxy-a" else "ok",
	)

	assert proxy_utils.resolve_for_url(settings, URL).host == "proxy-b"


def test_relay_side_errors_stop_the_candidates_at_once():
	attempts = []

	class RelayError(Exception):
		status_code = 404

	def opener(candidate):
		attempts.append(candidate.host)
		raise RelayError("not found")

	with pytest.raises(RelayError):
		proxy_utils.open_through_candidates(_settings_with_alternatives(), URL, opener)

	assert attempts == ["proxy-a"]


def test_all_candidates_failing_raises_the_first_error_and_forgets_the_url(detect):
	detect(windows_proxy.DetectedProxy(host="proxy-a", port=8080, type="http"))
	settings = proxy_utils.ProxySettings(auto_detect=True)
	resolved = proxy_utils.resolve_for_url(settings, URL)
	resolved = proxy_utils._append_alternative(
		resolved,
		proxy_utils.ProxySettings(host="proxy-b", port=8080, type="http"),
	)

	def opener(candidate):
		raise OSError("down: " + candidate.host)

	with pytest.raises(OSError, match="proxy-a"):
		proxy_utils.open_through_candidates(resolved, URL, opener)
	assert proxy_utils._cached(URL) is None


# --- 407 detection -----------------------------------------------------------------


def test_only_the_websocket_proxy_exception_text_counts_as_a_407():
	proxy_exception = type("WebSocketProxyException", (Exception,), {})
	other_exception = type("WebSocketBadStatusException", (Exception,), {})
	settings = proxy_utils.ProxySettings(host="proxy-a", port=8080, type="http")

	assert proxy_utils.needs_windows_authentication(
		settings,
		proxy_exception("failed CONNECT via proxy status: 407"),
	)
	assert not proxy_utils.needs_windows_authentication(settings, other_exception("Handshake status 407"))
	assert not proxy_utils.needs_windows_authentication(settings, ValueError("page 407 of the manual"))
	assert proxy_utils.needs_windows_authentication(settings, SimpleNamespace(status=407))


# --- SSPI: Negotiate then NTLM -------------------------------------------------------


class ScriptedSocket:
	def __init__(self, responses):
		self.responses = list(responses)
		self.received = bytearray(self.responses.pop(0) if self.responses else b"")
		self.sent = []
		self.closed = False

	def recv(self, size):
		chunk, self.received = bytes(self.received[:size]), self.received[size:]
		return chunk

	def sendall(self, data):
		self.sent.append(data)
		if self.responses:
			self.received.extend(self.responses.pop(0))

	def settimeout(self, timeout):
		pass

	def close(self):
		self.closed = True


def test_negotiate_failure_retries_with_ntlm_when_the_proxy_offers_it(monkeypatch):
	mechanisms = []

	class FakeAuthenticator:
		def __init__(self, mechanism, username="", password="", proxy_host=""):
			mechanisms.append((mechanism, proxy_host))
			self.mechanism = mechanism

		def initial_token(self):
			if self.mechanism == "negotiate":
				raise sspi_proxy.SSPIProxyError("Kerberos refused")
			return b"type1"

		def next_token(self, challenge):
			return b"type3"

		def close(self):
			pass

	sockets = [
		ScriptedSocket(
			[b"HTTP/1.1 407 Denied\r\nProxy-Authenticate: Negotiate, NTLM\r\nContent-Length: 0\r\n\r\n"],
		),
		ScriptedSocket(
			[
				b"HTTP/1.1 407 Denied\r\nProxy-Authenticate: Negotiate, NTLM\r\nContent-Length: 0\r\n\r\n",
				b"HTTP/1.1 407 Denied\r\nProxy-Authenticate: NTLM %s\r\nContent-Length: 0\r\n\r\n"
				% base64.b64encode(b"challenge"),
				b"HTTP/1.1 200 Connection Established\r\n\r\n",
			],
		),
	]
	monkeypatch.setattr(sspi_proxy, "SSPIProxyAuthenticator", FakeAuthenticator)
	monkeypatch.setattr(sspi_proxy.socket, "create_connection", lambda address, timeout: sockets.pop(0))
	settings = SimpleNamespace(type="negotiate", host="proxy.example", port=8080, username="", password="")

	result = sspi_proxy.open_sspi_proxy_tunnel(settings, "relay.example", 443, timeout=5)

	assert result.closed is False
	assert [mechanism for mechanism, _host in mechanisms] == ["negotiate", "ntlm"]
	assert all(host == "proxy.example" for _mechanism, host in mechanisms)


def test_negotiate_failure_is_final_when_ntlm_is_not_offered(monkeypatch):
	class FakeAuthenticator:
		def __init__(self, *args, **kwargs):
			pass

		def initial_token(self):
			raise sspi_proxy.SSPIProxyError("Kerberos refused")

		def close(self):
			pass

	sock = ScriptedSocket(
		[b"HTTP/1.1 407 Denied\r\nProxy-Authenticate: Negotiate\r\nContent-Length: 0\r\n\r\n"],
	)
	monkeypatch.setattr(sspi_proxy, "SSPIProxyAuthenticator", FakeAuthenticator)
	monkeypatch.setattr(sspi_proxy.socket, "create_connection", lambda address, timeout: sock)
	settings = SimpleNamespace(type="negotiate", host="proxy.example", port=8080, username="", password="")

	with pytest.raises(sspi_proxy.SSPIProxyError, match="Kerberos refused"):
		sspi_proxy.open_sspi_proxy_tunnel(settings, "relay.example", 443, timeout=5)
	assert sock.closed is True
