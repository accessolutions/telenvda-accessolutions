import base64
import importlib.util
from pathlib import Path
import socket
import ssl
import sys
from types import ModuleType

import pytest


PACKAGE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient"
PACKAGE_NAME = "connectivity_test_support"


class FakeResponse:
	def __init__(self, status, body=b"", headers=None):
		self.status = status
		self.body = body
		self.headers = headers or []

	def read(self):
		return self.body

	def getheaders(self):
		return self.headers


class FakeConnection:
	def __init__(self, responses):
		self.responses = list(responses)
		self.requests = []
		self.closed = False

	def request(self, method, path, body=None, headers=None):
		self.requests.append((method, path, body, headers or {}))

	def getresponse(self):
		return self.responses.pop(0)

	def close(self):
		self.closed = True


def _load_connectivity_module():
	package = ModuleType(PACKAGE_NAME)
	package.__path__ = [str(PACKAGE_PATH)]
	sys.modules[PACKAGE_NAME] = package

	log_module = ModuleType(f"{PACKAGE_NAME}.connectivity_log")
	log_module.entries = []
	log_module.write_result = lambda result: log_module.entries.append(dict(result))
	sys.modules[log_module.__name__] = log_module

	configuration = ModuleType(f"{PACKAGE_NAME}.configuration")
	configuration.get_config = lambda: {"controlserver": {"proxy_mode": "none"}}
	sys.modules[configuration.__name__] = configuration

	transport = ModuleType(f"{PACKAGE_NAME}.transport")
	transport.WebSocketRelayTransport = object
	sys.modules[transport.__name__] = transport

	for module_name in ("https_protocol", "https_connection", "proxy_utils"):
		spec = importlib.util.spec_from_file_location(
			f"{PACKAGE_NAME}.{module_name}",
			PACKAGE_PATH / f"{module_name}.py",
		)
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		sys.modules[spec.name] = module
		spec.loader.exec_module(module)

	spec = importlib.util.spec_from_file_location(
		f"{PACKAGE_NAME}.connectivity_test",
		PACKAGE_PATH / "connectivity_test.py",
	)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module, log_module


def _session_response(protocol="nvdaremote-http/1"):
	session_id = base64.urlsafe_b64encode(bytes(range(16))).decode("ascii").rstrip("=")
	token = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
	return (
		b'{"sessionId":"%s","token":"%s","protocol":"%s",'
		b'"pollWaitSeconds":25,"maxBatchMessages":32,"maxMessageBytes":1048576}'
		% (session_id.encode("ascii"), token.encode("ascii"), protocol.encode("ascii"))
	), token


def _prepare_success(monkeypatch):
	module, log_module = _load_connectivity_module()
	monkeypatch.setattr(module.socket, "getaddrinfo", lambda *args: [])
	body, token = _session_response()
	connection = FakeConnection([FakeResponse(201, body), FakeResponse(204)])
	monkeypatch.setattr(module.https_connection, "open_https_connection", lambda *args, **kwargs: connection)
	return module, log_module, connection, token


def test_https_connectivity_creates_and_deletes_session_without_websocket_headers(monkeypatch):
	module, log_module, connection, token = _prepare_success(monkeypatch)

	result = module.test_endpoint("relay.example", 443, "https", "/remote/http/v1", timeout=3)

	assert result["success"] is True
	assert "error_code" not in result
	assert [request[0] for request in connection.requests] == ["POST", "DELETE"]
	assert "Authorization" not in connection.requests[0][3]
	assert connection.requests[1][3]["Authorization"] == "Bearer " + token
	assert all("Upgrade" not in request[3] for request in connection.requests)
	assert connection.closed is True
	assert log_module.entries[0]["success"] is True
	assert token not in str(log_module.entries[0])


def test_https_connectivity_reports_dns_failure(monkeypatch):
	module, log_module = _load_connectivity_module()
	monkeypatch.setattr(module.socket, "getaddrinfo", lambda *args: (_ for _ in ()).throw(socket.gaierror()))

	result = module.test_endpoint("missing.example", 443, "https")

	assert result["error_code"] == "dns_unavailable"
	assert log_module.entries[0]["error_code"] == "dns_unavailable"


def test_https_connectivity_reports_proxy_authentication(monkeypatch):
	module, _ = _load_connectivity_module()
	monkeypatch.setattr(module.socket, "getaddrinfo", lambda *args: [])
	proxy_error = module.https_connection.HTTPSProxyAuthenticationError(407)
	monkeypatch.setattr(
		module.https_connection,
		"open_https_connection",
		lambda *args, **kwargs: (_ for _ in ()).throw(proxy_error),
	)

	result = module.test_endpoint("relay.example", 443, "https")

	assert result["error_code"] == "proxy_authentication_required"
	assert result["status"] == 407


def test_https_connectivity_reports_untrusted_certificate(monkeypatch):
	module, _ = _load_connectivity_module()
	monkeypatch.setattr(module.socket, "getaddrinfo", lambda *args: [])
	monkeypatch.setattr(
		module.https_connection,
		"open_https_connection",
		lambda *args, **kwargs: (_ for _ in ()).throw(ssl.SSLCertVerificationError("untrusted")),
	)

	result = module.test_endpoint("relay.example", 443, "https")

	assert result["error_code"] == "certificate_untrusted"


@pytest.mark.parametrize(
	("status", "error_code"),
	((403, "http_forbidden"), (404, "api_unavailable"), (429, "server_limit")),
)
def test_https_connectivity_reports_http_statuses(monkeypatch, status, error_code):
	module, _ = _load_connectivity_module()
	monkeypatch.setattr(module.socket, "getaddrinfo", lambda *args: [])
	connection = FakeConnection([FakeResponse(status, b'{"error":"unavailable"}')])
	monkeypatch.setattr(module.https_connection, "open_https_connection", lambda *args, **kwargs: connection)

	result = module.test_endpoint("relay.example", 443, "https")

	assert result["error_code"] == error_code
	assert result["status"] == status


def test_https_connectivity_reports_html_response(monkeypatch):
	module, _ = _load_connectivity_module()
	monkeypatch.setattr(module.socket, "getaddrinfo", lambda *args: [])
	connection = FakeConnection([FakeResponse(200, b"<html><body>blocked</body></html>", [("Content-Type", "text/html")])])
	monkeypatch.setattr(module.https_connection, "open_https_connection", lambda *args, **kwargs: connection)

	result = module.test_endpoint("relay.example", 443, "https")

	assert result["error_code"] == "html_response"