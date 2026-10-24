import importlib
import json
from pathlib import Path
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


_install_import_stubs()
e2e = importlib.import_module("remoteClient.e2e")
transport_module = importlib.import_module("remoteClient.transport")


class PlainSerializer:
	def serialize(self, type=None, **obj):
		obj["type"] = type
		return json.dumps(obj).encode("utf-8") + b"\n"

	def deserialize(self, data):
		return json.loads(data)


pytestmark = pytest.mark.skipif(not e2e.available(), reason="the bundled cryptography cannot be loaded")


class Clock:
	def __init__(self):
		self.now = 1000.0

	def __call__(self):
		return self.now


class Relay:
	"""Deliver the control messages of one side to the other, stamped with their origin."""

	def __init__(self):
		self.sides = {}
		self.sent = []

	def attach(self, client_id, peer_encryption):
		self.sides[client_id] = peer_encryption

	def writer(self, client_id):
		def write(type, **fields):
			message = dict(type=type, origin=client_id, **fields)
			self.sent.append(message)
			for other_id, other in self.sides.items():
				if other_id != client_id:
					other.observe(json.loads(json.dumps(message)))

		return write


def _pair(clock, timeout=3.0, slave_type="slave", master_type="master"):
	relay = Relay()
	flushed = {1: [], 2: []}
	slave = e2e.PeerEncryption(
		write=relay.writer(1),
		flush=flushed[1].extend,
		connection_type=lambda: slave_type,
		clock=clock,
		timeout=timeout,
	)
	master = e2e.PeerEncryption(
		write=relay.writer(2),
		flush=flushed[2].extend,
		connection_type=lambda: master_type,
		clock=clock,
		timeout=timeout,
	)
	relay.attach(1, slave)
	relay.attach(2, master)
	return relay, slave, master, flushed


def _join(relay, slave, master):
	"""The controlled computer is alone, then the controlling one arrives."""
	slave.observe({"type": "channel_joined", "origin": 1, "clients": []})
	assert slave.state() == e2e.CLEAR
	slave.observe({"type": "client_joined", "client": {"id": 2, "connection_type": "master"}})
	master.observe(
		{"type": "channel_joined", "origin": 2, "clients": [{"id": 1, "connection_type": "slave"}]},
	)


def test_peers_exchange_sending_keys_and_read_each_other():
	clock = Clock()
	relay, slave, master, _ = _pair(clock)
	_join(relay, slave, master)

	assert [message["type"] for message in relay.sent] == [e2e.MSG_HELLO, e2e.MSG_KEY, e2e.MSG_KEY]
	assert slave.state() == e2e.ENCRYPTED
	assert master.state() == e2e.ENCRYPTED
	assert slave.secure and master.secure

	envelope = slave.seal(b'{"type": "speak"}')
	assert envelope["kid"] == slave.kid
	assert master.open(envelope) == b'{"type": "speak"}'
	assert slave.open(envelope) is None, "a client never holds its own sending key as a peer key"
	assert slave.open(master.seal(b"hello")) == b"hello"

	# The relay never saw a sending key in clear.
	for message in relay.sent:
		serialized = json.dumps(message)
		assert e2e._b64(slave.sending_key) not in serialized
		assert e2e._b64(master.sending_key) not in serialized


def test_tampered_or_foreign_messages_are_rejected():
	clock = Clock()
	relay, slave, master, _ = _pair(clock)
	_join(relay, slave, master)

	envelope = slave.seal(b"secret")
	tampered = dict(envelope, data=envelope["data"][:-4] + "AAAA")
	assert master.open(tampered) is None
	assert master.open(dict(envelope, kid="unknown")) is None

	# A key addressed to someone else is ignored, a tampered one too.
	stranger = e2e.PeerEncryption(write=lambda *a, **k: None, connection_type=lambda: "master", clock=clock)
	before = dict(master._received_keys)
	key_message = next(m for m in relay.sent if m["type"] == e2e.MSG_KEY and m["origin"] == 1)
	master.observe(dict(key_message, to=e2e._b64(stranger.public_key)))
	master.observe(dict(key_message, tag=key_message["tag"][:-4] + "AAAA"))
	assert master._received_keys == before


def test_a_peer_which_never_greets_leaves_the_session_readable():
	clock = Clock()
	relay = Relay()
	slave = e2e.PeerEncryption(write=relay.writer(1), connection_type=lambda: "slave", clock=clock)
	relay.attach(1, slave)
	slave.observe({"type": "channel_joined", "origin": 1, "clients": []})
	slave.observe({"type": "client_joined", "client": {"id": 2, "connection_type": "master"}})

	assert slave.state() == e2e.PENDING
	clock.now += e2e.HANDSHAKE_TIMEOUT - 0.1
	assert slave.state() == e2e.PENDING
	clock.now += 0.2
	assert slave.state() == e2e.CLEAR
	assert not slave.secure

	# A late greeting still upgrades the session.
	late = e2e.PeerEncryption(write=relay.writer(2), connection_type=lambda: "master", clock=clock)
	relay.attach(2, late)
	late.observe({"type": "channel_joined", "origin": 2, "clients": [{"id": 1, "connection_type": "slave"}]})
	assert slave.state() == e2e.ENCRYPTED
	assert late.state() == e2e.ENCRYPTED


def test_one_legacy_peer_among_several_keeps_everything_readable():
	clock = Clock()
	relay, slave, master, _ = _pair(clock)
	_join(relay, slave, master)
	slave.observe({"type": "client_joined", "client": {"id": 3, "connection_type": "master"}})
	assert slave.state() == e2e.PENDING
	clock.now += e2e.HANDSHAKE_TIMEOUT
	assert slave.state() == e2e.CLEAR
	slave.observe({"type": "client_left", "client": {"id": 3, "connection_type": "master"}})
	assert slave.state() == e2e.ENCRYPTED


def test_only_the_other_side_of_the_channel_is_waited_for():
	clock = Clock()
	relay, slave, master, _ = _pair(clock)
	_join(relay, slave, master)
	# The relay never forwards a master's messages to another master.
	master.observe({"type": "client_joined", "client": {"id": 3, "connection_type": "master"}})
	assert master.state() == e2e.ENCRYPTED


def test_leaving_peer_takes_its_key_away():
	clock = Clock()
	relay, slave, master, _ = _pair(clock)
	_join(relay, slave, master)
	envelope = master.seal(b"bye")
	slave.observe({"type": "client_left", "client": {"id": 2, "connection_type": "master"}})
	assert slave.state() == e2e.CLEAR
	assert slave.open(envelope) is None


def test_deferred_messages_are_released_when_the_greeting_is_answered():
	clock = Clock()
	relay, slave, master, flushed = _pair(clock)
	slave.observe({"type": "channel_joined", "origin": 1, "clients": []})
	slave.observe({"type": "client_joined", "client": {"id": 2, "connection_type": "master"}})
	assert slave.state() == e2e.PENDING
	slave.defer(b"first")
	slave.defer(b"second")
	assert flushed[1] == []

	master.observe(
		{"type": "channel_joined", "origin": 2, "clients": [{"id": 1, "connection_type": "slave"}]},
	)
	assert slave.state() == e2e.ENCRYPTED
	assert flushed[1] == [b"first", b"second"]
	assert slave._timer is None


def test_deferred_messages_are_released_by_the_timer_when_nobody_answers():
	clock = Clock()
	released = threading.Event()
	items = []

	def flush(messages):
		items.extend(messages)
		released.set()

	slave = e2e.PeerEncryption(write=lambda *a, **k: None, flush=flush, clock=clock, timeout=0.05)
	slave.observe({"type": "channel_joined", "origin": 1, "clients": []})
	slave.observe({"type": "client_joined", "client": {"id": 2, "connection_type": "master"}})
	slave.defer(b"held")
	assert released.wait(2)
	assert items == [b"held"]


def test_a_new_join_starts_from_fresh_keys():
	clock = Clock()
	relay, slave, master, _ = _pair(clock)
	_join(relay, slave, master)
	public_key, sending_key = slave.public_key, slave.sending_key
	slave.observe({"type": "channel_joined", "origin": 1, "clients": []})
	assert slave.public_key != public_key
	assert slave.sending_key != sending_key
	assert slave.state() == e2e.CLEAR


def test_sealing_for_one_peer_requires_that_it_holds_our_key():
	clock = Clock()
	relay, slave, master, _ = _pair(clock)
	slave.observe({"type": "channel_joined", "origin": 1, "clients": []})
	slave.observe({"type": "client_joined", "client": {"id": 2, "connection_type": "master"}})
	assert slave.seal_for(2, b"offer") is None
	assert not slave.expects_sealed(2)
	master.observe(
		{"type": "channel_joined", "origin": 2, "clients": [{"id": 1, "connection_type": "slave"}]},
	)
	sealed = slave.seal_for(2, b"offer")
	assert sealed is not None and master.open(sealed) == b"offer"
	assert slave.expects_sealed(2)


# Whole transports wired through a pretend relay.


class WireTransport(transport_module.TCPTransport):
	"""A transport whose wire is a list, so that what the relay sees can be checked."""

	def __init__(self, connection_type, **kwargs):
		super().__init__(serializer=PlainSerializer(), address=("relay", 6837), **kwargs)
		self.connection_type = connection_type
		self.wire = []
		self.received = []
		self.connected = True
		self.callback_manager.register_callback("*", self._record)

	def _record(self, callback, **kwargs):
		if isinstance(callback, str):
			self.received.append((callback, kwargs))

	def write(self, obj):
		self.wire.append(obj)


class WireRelay:
	"""Forward what each transport writes to the other one, like the relay does."""

	def __init__(self, first, second):
		self.ends = {1: first, 2: second}
		self.seen = []

	def pump(self):
		moved = True
		while moved:
			moved = False
			for client_id, end in self.ends.items():
				while end.wire:
					moved = True
					raw = end.wire.pop(0)
					message = json.loads(raw)
					self.seen.append(message)
					if message["type"] in ("protocol_version", "join"):
						continue
					message["origin"] = client_id
					for other_id, other in self.ends.items():
						if other_id != client_id:
							other.parse(json.dumps(message).encode("utf-8"))


def _connected_pair(monkeypatch, timeout=3.0):
	clock = Clock()
	monkeypatch.setattr(e2e, "HANDSHAKE_TIMEOUT", timeout)
	slave = WireTransport("slave")
	master = WireTransport("master")
	for end in (slave, master):
		assert end.peer_encryption is not None
		end.peer_encryption._clock = clock
		end.peer_encryption.timeout = timeout
	relay = WireRelay(slave, master)
	slave.parse(json.dumps({"type": "channel_joined", "origin": 1, "clients": []}).encode())
	slave.parse(
		json.dumps({"type": "client_joined", "client": {"id": 2, "connection_type": "master"}}).encode(),
	)
	master.parse(
		json.dumps(
			{"type": "channel_joined", "origin": 2, "clients": [{"id": 1, "connection_type": "slave"}]},
		).encode(),
	)
	relay.pump()
	return clock, relay, slave, master


def test_transports_hide_their_messages_from_the_relay(monkeypatch):
	clock, relay, slave, master = _connected_pair(monkeypatch)
	assert slave.peer_encryption.secure and master.peer_encryption.secure

	slave.send("speak", sequence=[{"text": "bonjour"}])
	master.send("key", vk_code=65, pressed=True)
	wire = [json.loads(raw) for end in (slave, master) for raw in end.wire]
	assert all(message["type"] == "encrypted" and "kid" in message for message in wire)
	assert all("bonjour" not in json.dumps(message) for message in wire)
	relay.pump()

	assert ("msg_speak", {"sequence": [{"text": "bonjour"}], "origin": 1}) in master.received
	assert ("msg_key", {"vk_code": 65, "pressed": True, "origin": 2}) in slave.received
	# The greetings and keys were consumed by the transports, nobody else saw them.
	assert not any(callback.startswith("msg_e2e") for callback, _ in slave.received + master.received)
	assert not any(callback == "msg_encrypted" for callback, _ in slave.received + master.received)


def test_presence_messages_still_reach_the_session(monkeypatch):
	clock, relay, slave, master = _connected_pair(monkeypatch)
	assert ("msg_client_joined", {"client": {"id": 2, "connection_type": "master"}}) in slave.received
	assert any(callback == "msg_channel_joined" for callback, _ in master.received)


def test_transport_holds_messages_back_until_the_greeting_is_answered(monkeypatch):
	clock = Clock()
	slave = WireTransport("slave")
	slave.peer_encryption._clock = clock
	slave.parse(json.dumps({"type": "channel_joined", "origin": 1, "clients": []}).encode())
	slave.parse(
		json.dumps({"type": "client_joined", "client": {"id": 2, "connection_type": "master"}}).encode(),
	)
	slave.send("speak", sequence=["secret"])
	assert slave.wire == [], "nothing goes out while the newcomer may still greet us"

	clock.now += e2e.HANDSHAKE_TIMEOUT + 1
	slave.peer_encryption.flush()
	assert [json.loads(raw)["type"] for raw in slave.wire] == ["speak"]
	# From now on the session is readable, as with an older client.
	slave.send("speak", sequence=["more"])
	assert json.loads(slave.wire[-1])["type"] == "speak"


def test_signalling_payload_is_sealed_but_routing_stays_readable(monkeypatch):
	clock, relay, slave, master = _connected_pair(monkeypatch)
	master.send("webrtc_offer", target=1, sdp="v=0 secret")
	message = json.loads(master.wire[-1])
	assert message["type"] == "webrtc_offer"
	assert message["target"] == 1
	assert "sdp" not in message
	assert message["enc"]["kid"] == master.peer_encryption.kid
	relay.pump()
	assert ("msg_webrtc_offer", {"target": 1, "origin": 2, "sdp": "v=0 secret"}) in slave.received

	# Clear signalling from a peer which shares keys with us is not trusted.
	slave.received.clear()
	slave.parse(json.dumps({"type": "webrtc_offer", "target": 1, "origin": 2, "sdp": "forged"}).encode())
	assert slave.received == []


def test_a_password_disables_the_automatic_negotiation():
	transport = WireTransport("slave", encryption_key="secret")
	assert transport.peer_encryption is None
	transport.send("speak", sequence=["x"])
	message = json.loads(transport.wire[-1])
	assert message["type"] == "encrypted" and "kid" not in message
	inner = json.loads(e2e.open_envelope(transport.encryption_hash, message))
	assert inner == {"type": "speak", "sequence": ["x"]}


def test_control_messages_are_never_sealed_or_held_back(monkeypatch):
	clock = Clock()
	slave = WireTransport("slave")
	slave.peer_encryption._clock = clock
	slave.parse(
		json.dumps(
			{"type": "channel_joined", "origin": 1, "clients": [{"id": 2, "connection_type": "master"}]},
		).encode(),
	)
	types = [json.loads(raw)["type"] for raw in slave.wire]
	assert types == [e2e.MSG_HELLO]
	slave.send("ping")
	slave.send("protocol_version", version=2)
	assert [json.loads(raw)["type"] for raw in slave.wire[1:]] == ["ping", "protocol_version"]
