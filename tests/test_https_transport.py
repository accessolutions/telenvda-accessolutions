import base64
import http.client
import http.server
import io
import importlib
import json
from pathlib import Path
import ssl
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest


PACKAGE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient"


def _install_import_stubs():
	build_version = ModuleType("buildVersion")
	build_version.version_year = 2026
	sys.modules.setdefault("buildVersion", build_version)

	wx = ModuleType("wx")
	wx.CallAfter = lambda callback, *args, **kwargs: callback(*args, **kwargs)
	sys.modules.setdefault("wx", wx)

	# The transport records its connections through the daily log, which asks NVDA
	# where its configuration lives before writing anything.
	global_vars = ModuleType("globalVars")
	global_vars.appArgs = SimpleNamespace(secure=False, launcher=False, configPath="")
	sys.modules.setdefault("globalVars", global_vars)

	remote_client = ModuleType("remoteClient")
	remote_client.__path__ = [str(PACKAGE_PATH)]
	sys.modules.setdefault("remoteClient", remote_client)

	configuration = ModuleType("remoteClient.configuration")
	configuration.get_config = lambda: {"controlserver": {"proxy_mode": "none"}}
	sys.modules.setdefault("remoteClient.configuration", configuration)

	sspi_proxy = ModuleType("remoteClient.sspi_proxy")
	sspi_proxy.open_sspi_proxy_tunnel = lambda *args, **kwargs: None
	sys.modules.setdefault("remoteClient.sspi_proxy", sspi_proxy)

	socket_utils = ModuleType("remoteClient.socket_utils")
	socket_utils.SERVER_PORT = 6837
	socket_utils.address_to_hostport = lambda address: "%s:%s" % address
	socket_utils.hostport_to_address = lambda address: tuple(address.rsplit(":", 1))
	sys.modules.setdefault("remoteClient.socket_utils", socket_utils)

	websocket = ModuleType("websocket")
	websocket.create_connection = lambda *args, **kwargs: None
	sys.modules.setdefault("websocket", websocket)

	cryptodome = ModuleType("Cryptodome")
	cipher = ModuleType("Cryptodome.Cipher")
	cipher.AES = SimpleNamespace(GCM=1)
	cryptodome.Cipher = cipher
	sys.modules.setdefault("Cryptodome", cryptodome)
	sys.modules.setdefault("Cryptodome.Cipher", cipher)


_install_import_stubs()
transport_module = importlib.import_module("remoteClient.transport")
https_protocol = importlib.import_module("remoteClient.https_protocol")
https_version = importlib.import_module("remoteClient.https_version")


class FakeSocket:
	def __init__(self):
		self.timeout = 60

	def gettimeout(self):
		return self.timeout

	def settimeout(self, value):
		self.timeout = value


class FakeResponse:
	def __init__(self, status, body=b"", headers=None):
		self.status = status
		self._body = body
		self._stream = io.BytesIO(body)
		self._headers = headers or []

	def read(self):
		return self._stream.read()

	def readline(self, size=-1):
		return self._stream.readline(size)

	def getheaders(self):
		return self._headers

	def getheader(self, name, default=None):
		name = name.lower()
		for key, value in self._headers:
			if key.lower() == name:
				return value
		return default

	def close(self):
		self._stream.close()


class FakeConnection:
	def __init__(self, responses):
		self.responses = list(responses)
		self.requests = []
		self.sock = FakeSocket()
		self.closed = False

	def request(self, method, path, body=None, headers=None):
		self.requests.append((method, path, body, headers or {}))

	def getresponse(self):
		return self.responses.pop(0)

	def close(self):
		self.closed = True


class FakeSerializer:
	def deserialize(self, value):
		return json.loads(value)


def _new_transport():
	transport = transport_module.HTTPSTransport(
		serializer=FakeSerializer(),
		address=("relay.example", 443),
		https_path="/remote/http/v1",
		insecure=True,
	)
	transport.connected = True
	transport.session_id = base64.urlsafe_b64encode(bytes(range(16))).decode("ascii").rstrip("=")
	transport.session_token = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
	return transport


def test_send_batch_uses_json_payloads_and_http_headers():
	transport = _new_transport()
	connection = FakeConnection(
		[
			FakeResponse(200, b'{"acceptedThrough":2}'),
		],
	)
	transport.send_connection = connection
	transport.max_batch_messages = 2
	transport.queue.put(b'{"type":"protocol_version","version":2}\n')
	transport.queue.put(b'{"type":"join","channel":"1234567","connection_type":"master"}\n')

	batch = transport._take_batch()
	transport._send_batch(batch)

	assert [message.sequence for message in batch] == [1, 2]
	method, path, body, headers = connection.requests[0]
	assert method == "POST"
	assert path.endswith("/messages")
	assert json.loads(body) == {
		"messages": [
			{"sequence": 1, "payload": {"type": "protocol_version", "version": 2}},
			{"sequence": 2, "payload": {"type": "join", "channel": "1234567", "connection_type": "master"}},
		],
	}
	assert headers["Authorization"].startswith("Bearer ")
	assert headers["X-TeleNVDA-Transport"] == "https-long-poll/1"
	assert "Upgrade" not in headers


def test_send_batch_carries_received_through():
	transport = _new_transport()
	connection = FakeConnection(
		[
			FakeResponse(200, b'{"acceptedThrough":1}'),
		],
	)
	transport.send_connection = connection
	transport.last_received_sequence = 4
	batch = [https_protocol.MessageEnvelope(1, {"type": "ping"})]

	transport._send_batch(batch)

	body = json.loads(connection.requests[0][2])
	assert body["receivedThrough"] == 4
	assert transport.acknowledged_sequence == 4


def test_send_acknowledgement_uses_an_empty_batch():
	transport = _new_transport()
	connection = FakeConnection(
		[
			FakeResponse(200, b'{"acceptedThrough":0}'),
		],
	)
	transport.send_connection = connection
	transport.last_received_sequence = 4

	transport._send_acknowledgement()

	body = json.loads(connection.requests[0][2])
	assert body == {"messages": [], "receivedThrough": 4}
	assert transport.acknowledged_sequence == 4


def test_run_creates_session_polls_and_deletes_it():
	transport = _new_transport()
	transport.connected = False
	session_id = transport.session_id
	send_connection = FakeConnection(
		[
			FakeResponse(
				201,
				json.dumps(
					{
						"sessionId": transport.session_id,
						"token": transport.session_token,
						"protocol": "nvdaremote-http/1",
						"pollWaitSeconds": 25,
						"maxBatchMessages": 32,
						"maxMessageBytes": 1048576,
					},
				).encode("utf-8"),
			),
			FakeResponse(204),
		],
	)
	poll_connection = FakeConnection(
		[
			FakeResponse(200, b'{"messages":[{"sequence":1,"payload":{"type":"ping"}}],"lastSequence":1}'),
		],
	)
	connections = iter((send_connection, poll_connection))
	transport._open_http_connection = lambda: next(connections)
	transport.callback_manager.register_callback("msg_ping", lambda: transport._stop_event.set())

	transport.run()

	assert send_connection.requests[0][0:2] == ("POST", "/remote/http/v1/sessions")
	assert poll_connection.requests[0][0] == "GET"
	assert "after=0" in poll_connection.requests[0][1]
	assert send_connection.requests[-1][0:2] == (
		"DELETE",
		"/remote/http/v1/sessions/%s" % session_id,
	)
	assert transport.session_id is None
	assert transport.session_token is None
	assert transport.last_received_sequence == 0


def test_stream_transport_negotiates_v2_and_receives_messages_after_ready():
	transport = transport_module.HTTPSStreamTransport(
		serializer=FakeSerializer(),
		address=("relay.example", 443),
		insecure=True,
	)
	assert transport.https_path == "/remote/http/v2"
	assert transport.PROTOCOL == "nvdaremote-http/2"
	session_id = base64.urlsafe_b64encode(bytes(range(16))).decode("ascii").rstrip("=")
	token = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
	send_connection = FakeConnection(
		[
			FakeResponse(
				201,
				json.dumps(
					{
						"sessionId": session_id,
						"token": token,
						"protocol": "nvdaremote-http/2",
						"pollWaitSeconds": 25,
						"maxBatchMessages": 32,
						"maxMessageBytes": 1048576,
						"streamKeepaliveSeconds": 15,
					},
				).encode("utf-8"),
			),
			FakeResponse(204),
		],
	)
	poll_connection = FakeConnection(
		[
			FakeResponse(
				200,
				b'event: ready\ndata: {}\n\nid: 1\ndata: {"type":"ping"}\n\n',
				[("Content-Type", "text/event-stream; charset=utf-8")],
			),
		],
	)
	connections = iter((send_connection, poll_connection))
	transport._open_http_connection = lambda: next(connections)
	transport.callback_manager.register_callback("msg_ping", lambda: transport._stop_event.set())

	transport.run()

	assert poll_connection.requests[0][0] == "GET"
	assert poll_connection.requests[0][1] == "/remote/http/v2/sessions/%s/stream?after=0" % session_id
	assert poll_connection.requests[0][3]["Accept"] == "text/event-stream"
	assert poll_connection.requests[0][3]["X-TeleNVDA-Transport"] == "https-stream/2"
	assert send_connection.requests[0][2] == b'{"protocol":"nvdaremote-http/2"}'
	assert send_connection.requests[-1][0:2] == ("DELETE", "/remote/http/v2/sessions/%s" % session_id)


def test_https_factory_falls_back_to_v1_for_the_same_route():
	https_version.forget_all()
	first = transport_module.create_https_relay_transport(
		address=("relay.example", 443),
		serializer=FakeSerializer(),
		https_path="/custom/http/v1",
		insecure=True,
	)

	assert isinstance(first, transport_module.HTTPSStreamRelayTransport)
	assert first.https_path == "/remote/http/v2"
	assert first.fall_back_to_v1() is True
	assert isinstance(first, transport_module.HTTPSRelayTransport)
	assert first.https_path == "/custom/http/v1"
	assert first.PROTOCOL == https_protocol.PROTOCOL
	assert first.TRANSPORT_HEADER == "https-long-poll/1"

	second = transport_module.create_https_relay_transport(
		address=("relay.example", 443),
		serializer=FakeSerializer(),
		https_path="/custom/http/v1",
		insecure=True,
	)
	assert isinstance(second, transport_module.HTTPSRelayTransport)
	https_version.forget_all()


def test_https_factory_retries_v1_after_a_local_server_rejects_v2():
	class FallbackHandler(http.server.BaseHTTPRequestHandler):
		protocol_version = "HTTP/1.1"

		def _reply(self, status, body=b""):
			self.send_response(status)
			self.send_header("Content-Type", "application/json")
			self.send_header("Content-Length", str(len(body)))
			self.end_headers()
			if body:
				self.wfile.write(body)

		def do_POST(self):
			self.server.requests.append((self.command, self.path))
			if self.path == "/remote/http/v2/sessions":
				self._reply(404, b"{}")
				return
			if self.path == "/remote/http/v1/sessions":
				body = json.dumps(
					{
						"sessionId": base64.urlsafe_b64encode(bytes(range(16))).decode("ascii").rstrip("="),
						"token": base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
						"protocol": "nvdaremote-http/1",
						"pollWaitSeconds": 25,
						"maxBatchMessages": 32,
						"maxMessageBytes": 1048576,
					},
				).encode("utf-8")
				self._reply(201, body)
				return
			if self.path.startswith("/remote/http/v1/sessions/") and self.path.endswith("/messages"):
				self._reply(200, b'{"acceptedThrough":0}')
				return
			self._reply(404, b"{}")

		def do_GET(self):
			self.server.requests.append((self.command, self.path))
			if self.path.startswith("/remote/http/v1/sessions/") and "/messages?" in self.path:
				self._reply(200, b'{"messages":[],"lastSequence":0}')
				return
			self._reply(404, b"{}")

		def do_DELETE(self):
			self.server.requests.append((self.command, self.path))
			self._reply(204)

		def log_message(self, format, *args):
			return

	server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FallbackHandler)
	server.requests = []
	server_thread = threading.Thread(target=server.serve_forever, daemon=True)
	server_thread.start()
	try:
		address = ("127.0.0.1", server.server_port)
		transport = transport_module.create_https_relay_transport(
			address=address,
			serializer=FakeSerializer(),
			insecure=True,
		)
		transport._open_http_connection = lambda: http.client.HTTPConnection(*address)
		connector = transport_module.ConnectorThread(transport, connect_delay=0)
		transport.callback_manager.register_callback(
			transport_module.TransportEvents.CONNECTED,
			lambda: (transport._stop_event.set(), setattr(connector, "running", False)),
		)
		connector.start()
		connector.join(timeout=5)

		assert not connector.is_alive()
		assert isinstance(transport, transport_module.HTTPSRelayTransport)
		assert transport.https_path == "/remote/http/v1"
		assert [path for method, path in server.requests if method == "POST"][:2] == [
			"/remote/http/v2/sessions",
			"/remote/http/v1/sessions",
		]
	finally:
		server.shutdown()
		server.server_close()
		server_thread.join(timeout=5)


def test_certificate_policy_reuses_trusted_fingerprint_for_both_connections(monkeypatch):
	transport = _new_transport()
	transport.insecure = False
	transport._proxy_settings = SimpleNamespace(enabled=False)
	trusted_fingerprint = "a" * 64
	configuration = sys.modules["remoteClient.configuration"]
	monkeypatch.setattr(transport_module, "hostport_to_address", lambda address: "relay.example:443")
	address_key = "relay.example:443"
	monkeypatch.setattr(
		configuration,
		"get_config",
		lambda: {"trusted_certs": {address_key: trusted_fingerprint}},
	)
	attempts = 0
	opened = []

	def open_connection():
		nonlocal attempts
		attempts += 1
		if attempts == 1:
			raise ssl.SSLCertVerificationError("untrusted")
		opened.append(transport.insecure)
		return SimpleNamespace(close=lambda: None)

	monkeypatch.setattr(transport, "_open_http_connection", open_connection)
	monkeypatch.setattr(
		transport_module.https_connection,
		"open_https_connection",
		lambda *args, **kwargs: SimpleNamespace(
			sock=SimpleNamespace(getpeercert=lambda binary_form: b"certificate"),
			close=lambda: None,
		),
	)
	monkeypatch.setattr(
		transport_module.https_connection,
		"peer_certificate_fingerprint",
		lambda connection: trusted_fingerprint,
	)

	first = transport._open_http_connection_with_certificate_policy()
	second = transport._open_http_connection_with_certificate_policy()

	assert first is not None
	assert second is not None
	assert transport.insecure is True
	assert opened == [True, True]


def test_unknown_certificate_emits_trust_event_and_preserves_fingerprint(monkeypatch):
	transport = _new_transport()
	transport.insecure = False
	transport._proxy_settings = SimpleNamespace(enabled=False)
	configuration = sys.modules["remoteClient.configuration"]
	monkeypatch.setattr(transport_module, "hostport_to_address", lambda address: "relay.example:443")
	monkeypatch.setattr(configuration, "get_config", lambda: {"trusted_certs": {}})
	fingerprint = "b" * 64
	events = []
	transport.callback_manager.register_callback(
		transport_module.TransportEvents.CERTIFICATE_AUTHENTICATION_FAILED,
		lambda: events.append(True),
	)
	monkeypatch.setattr(
		transport,
		"_open_http_connection",
		lambda: (_ for _ in ()).throw(ssl.SSLCertVerificationError("untrusted")),
	)
	monkeypatch.setattr(
		transport_module.https_connection,
		"open_https_connection",
		lambda *args, **kwargs: SimpleNamespace(close=lambda: None),
	)
	monkeypatch.setattr(
		transport_module.https_connection,
		"peer_certificate_fingerprint",
		lambda connection: fingerprint,
	)

	with pytest.raises(ssl.SSLCertVerificationError):
		transport._open_http_connection_with_certificate_policy()

	assert events == [True]
	assert transport.last_fail_fingerprint == fingerprint
	assert transport.insecure is False


def test_http_error_does_not_trigger_certificate_callback(monkeypatch):
	transport = _new_transport()
	transport._proxy_settings = SimpleNamespace(enabled=False)
	callbacks = []
	transport.callback_manager.register_callback(
		transport_module.TransportEvents.CERTIFICATE_AUTHENTICATION_FAILED,
		lambda: callbacks.append(True),
	)
	monkeypatch.setattr(
		transport,
		"_open_http_connection",
		lambda: (_ for _ in ()).throw(transport_module._HTTPSRequestError(407, "proxy authentication")),
	)

	with pytest.raises(transport_module._HTTPSRequestError):
		transport._open_http_connection_with_certificate_policy()

	assert callbacks == []


def test_messages_and_connection_events_reach_the_daily_log(tmp_path):
	traffic_log = transport_module.traffic_log
	settings = {"enabled": True, "traffic": True, "retention_days": 30}
	assert traffic_log.initialize(settings, str(tmp_path)) is True
	message = b'{"type":"speak","sequence":["a secret spoken out loud"]}'
	try:
		transport = _new_transport()
		transport.parse(message)
		transport._fire(transport_module.TransportEvents.DISCONNECTED)
	finally:
		traffic_log.terminate()

	content = next(tmp_path.glob("telenvda-*.log")).read_text(encoding="utf-8")
	assert "traffic in type=speak bytes=%d transport=HTTPSTransport" % len(message) in content
	assert "a secret spoken out loud" not in content
	assert "connection transport_disconnected transport=HTTPSTransport address=relay.example:443" in content
