import base64
import importlib.util
from pathlib import Path
import sys

import pytest


PACKAGE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient"
# The modules are loaded outside of their package, so their siblings must be importable.
sys.path.insert(0, str(PACKAGE_PATH))


def _load(module_name):
	spec = importlib.util.spec_from_file_location(module_name, PACKAGE_PATH / f"{module_name}.py")
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


proxy_tunnel = _load("proxy_tunnel")
sspi_proxy = _load("sspi_proxy")


class ScriptedSocket:
	"""Socket returning queued proxy responses and recording the requests."""

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
		self.timeout = timeout

	def close(self):
		self.closed = True


class FakeAuthenticator:
	def __init__(self, mechanism, username="", password="", target_host=""):
		self.mechanism = mechanism
		self.target_host = target_host
		self.challenges = []
		self.closed = False

	def initial_token(self):
		return b"type1"

	def next_token(self, challenge):
		self.challenges.append(challenge)
		return b"type3"

	def close(self):
		self.closed = True


def test_connect_request_brackets_ipv6_and_carries_credentials():
	request = proxy_tunnel.build_connect_request("::1", 443, "Basic dXNlcjpwYXNz")

	assert request.startswith(b"CONNECT [::1]:443 HTTP/1.1\r\n")
	assert b"Host: [::1]:443\r\n" in request
	assert b"Proxy-Authorization: Basic dXNlcjpwYXNz\r\n" in request
	assert request.endswith(b"\r\n\r\n")
	assert b"Proxy-Authorization" not in proxy_tunnel.build_connect_request("relay.example", 443)


def test_connect_response_leaves_the_tunnelled_stream_untouched():
	handshake = b"\x16\x03\x01first-tls-bytes"
	sock = ScriptedSocket([b"HTTP/1.1 200 Connection Established\r\nVia: squid\r\n\r\n" + handshake])

	status, headers = proxy_tunnel.read_connect_response(sock)

	assert (status, headers) == (200, {"via": "squid"})
	assert sock.recv(len(handshake)) == handshake


def test_rejected_response_body_is_drained_before_the_next_request():
	sock = ScriptedSocket([b"HTTP/1.1 407 Denied\r\nContent-Length: 5\r\n\r\nerror"])

	status, headers = proxy_tunnel.read_connect_response(sock)
	proxy_tunnel.drain_response_body(sock, headers)

	assert status == 407
	assert sock.recv(16) == b""


def test_truncated_response_raises_the_caller_error_type():
	class CallerError(Exception):
		pass

	with pytest.raises(CallerError):
		proxy_tunnel.read_connect_response(ScriptedSocket([b"HTTP/1.1 200"]), CallerError)


def test_sspi_tunnel_completes_a_multi_round_exchange_with_error_pages(monkeypatch):
	challenge = b"ntlm-challenge"
	sock = ScriptedSocket(
		[
			b"HTTP/1.1 407 Proxy Authentication Required\r\n"
			b"Proxy-Authenticate: NTLM\r\nContent-Length: 9\r\n\r\nfirst 407",
			b"HTTP/1.1 407 Proxy Authentication Required\r\n"
			b"Proxy-Authenticate: NTLM %s\r\nContent-Length: 10\r\n\r\nsecond 407"
			% base64.b64encode(challenge),
			b"HTTP/1.1 200 Connection Established\r\n\r\n",
		],
	)
	monkeypatch.setattr(sspi_proxy, "SSPIProxyAuthenticator", FakeAuthenticator)
	monkeypatch.setattr(sspi_proxy.socket, "create_connection", lambda address, timeout: sock)
	settings = type(
		"Settings",
		(),
		{"type": "ntlm", "host": "proxy.example", "port": 8080, "username": "", "password": ""},
	)()

	result = sspi_proxy.open_sspi_proxy_tunnel(settings, "relay.example", 443, timeout=5)

	assert result is sock
	assert sock.closed is False
	authorizations = [
		line.split(b": ", 1)[1].decode("ascii")
		for request in sock.sent
		for line in request.split(b"\r\n")
		if line.startswith(b"Proxy-Authorization")
	]
	assert authorizations == [
		"NTLM " + base64.b64encode(b"type1").decode("ascii"),
		"NTLM " + base64.b64encode(b"type3").decode("ascii"),
	]
	assert len(sock.sent) == 3
