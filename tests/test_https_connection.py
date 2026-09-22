import base64
import hashlib
import importlib.util
import io
import http.client
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


MODULE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient/https_connection.py"
SPEC = importlib.util.spec_from_file_location("https_connection", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
https_connection = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = https_connection
SPEC.loader.exec_module(https_connection)


class FakeSocket:
	def __init__(self, received=b""):
		self.received = received
		self.response = b""
		self.sent = []
		self.closed = False

	def recv(self, size):
		chunk, self.received = self.received[:size], self.received[size:]
		return chunk

	def sendall(self, data):
		self.sent.append(data)

	def settimeout(self, timeout):
		self.timeout = timeout

	def close(self):
		self.closed = True

	def makefile(self, mode, buffering=None):
		return io.BytesIO(self.response)


class FakeTLSContext:
	def __init__(self):
		self.minimum_version = None
		self.check_hostname = None
		self.verify_mode = None

	def load_default_certs(self):
		self.loaded_defaults = True

	def wrap_socket(self, raw_socket, server_hostname):
		self.server_hostname = server_hostname
		return raw_socket


class FakePeerSocket:
	def __init__(self, certificate):
		self.certificate = certificate

	def getpeercert(self, binary_form=False):
		return self.certificate if binary_form else {}


def _settings(**values):
	defaults = {
		"host": "",
		"port": 0,
		"type": "http",
		"username": "",
		"password": "",
		"enabled": False,
		"use_environment": False,
	}
	defaults.update(values)
	return SimpleNamespace(**defaults)


def test_direct_connection_uses_tls_and_ignores_proxy_environment(monkeypatch):
	raw_socket = FakeSocket()
	tls_context = FakeTLSContext()

	monkeypatch.setattr(https_connection.socket, "create_connection", lambda address, timeout: raw_socket)
	monkeypatch.setattr(https_connection.ssl, "SSLContext", lambda protocol: tls_context)

	connection = https_connection.open_https_connection(
		("relay.example", 443),
		_settings(),
		timeout=7,
		insecure=True,
	)

	assert connection.sock is raw_socket
	assert tls_context.server_hostname == "relay.example"
	assert tls_context.minimum_version == https_connection.ssl.TLSVersion.TLSv1_2
	assert tls_context.verify_mode == https_connection.ssl.CERT_NONE

	connection.close()
	assert raw_socket.closed is True


def test_automatic_proxy_settings_are_resolved_for_the_destination(monkeypatch):
	raw_socket = FakeSocket()
	tls_context = FakeTLSContext()
	resolved = _settings(host="proxy.example", port=8080, enabled=True)
	settings = _settings(auto_detect=True)
	calls = []
	proxy_utils = SimpleNamespace(
		resolve_for_url=lambda value, url: calls.append((value, url)) or resolved,
	)
	monkeypatch.setitem(sys.modules, "proxy_utils", proxy_utils)
	monkeypatch.setattr(https_connection.socket, "create_connection", lambda *args: raw_socket)
	monkeypatch.setattr(https_connection.ssl, "SSLContext", lambda protocol: tls_context)
	monkeypatch.setattr(https_connection, "_open_proxy_socket", lambda value, host, port, timeout: raw_socket)

	connection = https_connection.open_https_connection(
		("relay.example", 443),
		settings,
		timeout=5,
		insecure=True,
	)

	assert calls == [(settings, "https://relay.example:443/")]
	assert connection.sock is raw_socket
	connection.close()


def test_http_407_without_credentials_retries_with_windows_authentication(monkeypatch):
	raw_socket = FakeSocket()
	tls_context = FakeTLSContext()
	settings = _settings(host="proxy.example", port=8080, enabled=True)
	sspi_settings = _settings(host="proxy.example", port=8080, enabled=True, type="negotiate")
	proxy_utils = SimpleNamespace(with_windows_authentication=lambda value: sspi_settings)
	monkeypatch.setitem(sys.modules, "proxy_utils", proxy_utils)
	monkeypatch.setattr(https_connection.ssl, "SSLContext", lambda protocol: tls_context)
	monkeypatch.setattr(
		https_connection,
		"_open_proxy_socket",
		lambda *args: (_ for _ in ()).throw(https_connection.HTTPSProxyAuthenticationError(407)),
	)
	used_settings = []
	monkeypatch.setattr(
		https_connection,
		"_open_sspi_tunnel",
		lambda value, *args: used_settings.append(value) or raw_socket,
	)

	connection = https_connection.open_https_connection(
		("relay.example", 443),
		settings,
		timeout=5,
		insecure=True,
	)

	assert used_settings == [sspi_settings]
	assert connection.sock is raw_socket
	connection.close()


def test_http_proxy_connect_supports_basic_authentication():
	proxy_socket = FakeSocket(b"HTTP/1.1 200 Connection Established\r\n\r\n")
	settings = _settings(
		host="proxy.example",
		port=8080,
		enabled=True,
		username="alice",
		password="secret",
	)

	result = https_connection._open_http_connect_tunnel(
		settings,
		"relay.example",
		443,
		timeout=5,
		socket_factory=lambda *_: proxy_socket,
	)

	assert result is proxy_socket
	request = proxy_socket.sent[0].decode("ascii")
	assert "CONNECT relay.example:443 HTTP/1.1" in request
	assert "Host: relay.example:443" in request
	authentication = base64.b64encode(b"alice:secret").decode("ascii")
	assert f"Proxy-Authorization: Basic {authentication}" in request


def test_http_proxy_407_and_truncated_responses_are_explicit_errors():
	settings = _settings(host="proxy.example", port=8080, enabled=True)
	unauthorized = FakeSocket(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
	with pytest.raises(https_connection.HTTPSProxyAuthenticationError) as error:
		https_connection._open_http_connect_tunnel(
			settings,
			"relay.example",
			443,
			timeout=5,
			socket_factory=lambda *_: unauthorized,
		)
	assert error.value.status == 407

	truncated = FakeSocket(b"HTTP/1.1 200")
	with pytest.raises(https_connection.HTTPSConnectionError):
		https_connection._open_http_connect_tunnel(
			settings,
			"relay.example",
			443,
			timeout=5,
			socket_factory=lambda *_: truncated,
		)


def test_http_connection_accepts_connection_close_and_reports_truncated_body(monkeypatch):
	raw_socket = FakeSocket()
	raw_socket.response = b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\nok"
	tls_context = FakeTLSContext()
	monkeypatch.setattr(https_connection.socket, "create_connection", lambda *args: raw_socket)
	monkeypatch.setattr(https_connection.ssl, "SSLContext", lambda protocol: tls_context)

	connection = https_connection.open_https_connection(
		("relay.example", 443),
		_settings(),
		timeout=5,
		insecure=True,
	)
	connection.request("GET", "/health")
	response = connection.getresponse()
	assert response.read() == b"ok"
	connection.close()

	raw_socket = FakeSocket()
	raw_socket.response = b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nok"
	monkeypatch.setattr(https_connection.socket, "create_connection", lambda *args: raw_socket)
	connection = https_connection.open_https_connection(
		("relay.example", 443),
		_settings(),
		timeout=5,
		insecure=True,
	)
	connection.request("GET", "/health")
	with pytest.raises(http.client.IncompleteRead):
		connection.getresponse().read()
	connection.close()


def test_peer_certificate_fingerprint_is_sha256():
	certificate = b"certificate-der"
	connection = SimpleNamespace(sock=FakePeerSocket(certificate))

	assert (
		https_connection.peer_certificate_fingerprint(connection) == hashlib.sha256(certificate).hexdigest()
	)


def test_only_certificate_verification_errors_trigger_certificate_policy():
	certificate_error = https_connection.ssl.SSLCertVerificationError("untrusted")
	assert https_connection.is_certificate_verification_error(certificate_error) is True
	assert (
		https_connection.is_certificate_verification_error(https_connection.ssl.SSLError("protocol failure"))
		is False
	)
	assert https_connection.is_certificate_verification_error(ValueError("invalid JSON")) is False
