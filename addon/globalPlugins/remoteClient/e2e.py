"""Automatic end-to-end encryption between the clients of a channel.

The relay only has to read a handful of messages to do its work: the ones which
join a channel, the presence of the other clients and the routing fields of the
screen sharing signalling. Everything else, speech, braille, keystrokes, the
clipboard and the audio, is sealed here so that the relay, its logs and its
database never see it.

Every client draws an ephemeral X25519 key pair and a random *sending key* at
each join. When it enters a channel it announces its public key with an
``e2e_hello`` message. Each peer answers with an ``e2e_key`` message which carries
the peer's own sending key, wrapped with the shared secret of the two public keys,
and the newcomer sends its own sending key back the same way. From then on, every
message a client sends is encrypted with its sending key in the usual ``encrypted``
envelope, with a ``kid`` field naming the key so that the receiver knows which
sending key to use.

A client which never answers the greeting does not know this protocol. The
session then stays readable, as it always was, so that an older add-on or the
official NVDA Remote client can still take part: the messages which would have
been sealed are held back for a few seconds while the greeting is pending, then
sent as they are.

Anyone who knows the key of the channel can join it and will be handed the
sending keys like any other peer. This protects the session against the relay,
not against someone who holds the channel key.
"""

import base64
import os
import sys
import threading
import time
from logging import getLogger

import buildVersion

_LIBRARY_PATH = os.path.join(
	os.path.abspath(os.path.dirname(__file__)),
	"lib64" if buildVersion.version_year >= 2026 else "lib32",
)
sys.path.append(_LIBRARY_PATH)
try:
	from Cryptodome.Cipher import AES
	from Cryptodome.Hash import SHA256
	from Cryptodome.Protocol.DH import import_x25519_public_key, key_agreement
	from Cryptodome.Protocol.KDF import HKDF
	from Cryptodome.PublicKey import ECC
	from Cryptodome.Random import get_random_bytes

	_AVAILABLE = hasattr(AES, "MODE_GCM")
except Exception:  # pragma: no cover - depends on the bundled library
	_AVAILABLE = False
finally:
	sys.path.remove(_LIBRARY_PATH)

log = getLogger("e2e")

#: Greeting broadcast by a client entering a channel. Fields: ``pub``.
MSG_HELLO = "e2e_hello"
#: Sending key handed to one peer. Fields: ``to``, ``pub``, ``kid``, ``nonce``, ``data``, ``tag``.
MSG_KEY = "e2e_key"
#: Messages of this protocol itself. They are the only ones never encrypted here.
CONTROL_TYPES = frozenset((MSG_HELLO, MSG_KEY))

#: Seconds a peer is given to answer the greeting before the session is deemed
#: to be shared with a client which does not know this protocol.
HANDSHAKE_TIMEOUT = 3.0

#: Outcome of :meth:`PeerEncryption.state`.
CLEAR = "clear"
PENDING = "pending"
ENCRYPTED = "encrypted"

_WRAP_CONTEXT = b"TeleNVDA end-to-end key wrap v1"
_KEY_LENGTH = 32
_KEY_ID_LENGTH = 8


def available():
	"""Return True when the bundled cryptography can run this protocol."""
	return _AVAILABLE


def _b64(data):
	return base64.b64encode(data).decode("ascii")


def _unb64(text):
	return base64.b64decode(text.encode("ascii"))


def seal(key, plaintext, associated_data=b""):
	"""Encrypt ``plaintext`` with AES-256-GCM and return the fields of an envelope."""
	cipher = AES.new(key, AES.MODE_GCM)
	if associated_data:
		cipher.update(associated_data)
	data, tag = cipher.encrypt_and_digest(plaintext)
	return {"nonce": _b64(cipher.nonce), "data": _b64(data), "tag": _b64(tag)}


def open_envelope(key, envelope, associated_data=b""):
	"""Return the plaintext of an envelope produced by :func:`seal`, or None when invalid."""
	try:
		cipher = AES.new(key, AES.MODE_GCM, nonce=_unb64(envelope["nonce"]))
		if associated_data:
			cipher.update(associated_data)
		return cipher.decrypt_and_verify(_unb64(envelope["data"]), _unb64(envelope["tag"]))
	except Exception:
		return None


class _Peer:
	"""What is known about one other client of the channel."""

	__slots__ = ("id", "public_key", "wrap_key", "key_sent", "kid", "since")

	def __init__(self, id, since):
		self.id = id
		self.public_key = None
		self.wrap_key = None
		#: Our sending key has been handed to this peer: it can read what we send.
		self.key_sent = False
		#: Identifier of the sending key received from this peer, if any.
		self.kid = None
		self.since = since


class PeerEncryption:
	"""Negotiate and apply the end-to-end encryption of one transport.

	``write`` sends a clear control message to the relay, ``flush`` is handed the
	messages held back during a greeting once their fate is known, and
	``connection_type`` returns the side this client is on so that only the
	clients it actually talks to are waited for.
	"""

	def __init__(
		self,
		write,
		flush=None,
		connection_type=lambda: None,
		clock=time.monotonic,
		timeout=HANDSHAKE_TIMEOUT,
	):
		self._write = write
		self._flush = flush
		self._connection_type = connection_type
		self._clock = clock
		self.timeout = timeout
		self._lock = threading.RLock()
		self._deferred = []
		self._timer = None
		self._last_state = None
		self.reset()

	# Key material

	def reset(self):
		"""Forget every peer and draw new keys, for a new connection or a new join."""
		with self._lock:
			self._private_key = ECC.generate(curve="Curve25519")
			self.public_key = self._private_key.public_key().export_key(format="raw")
			self.sending_key = get_random_bytes(_KEY_LENGTH)
			self.kid = _b64(get_random_bytes(_KEY_ID_LENGTH))
			self._peers = {}
			self._received_keys = {}
			self._deferred = []
			self._last_state = None
			self._cancel_timer()

	def _wrap_key_for(self, peer_public_key):
		"""Derive the key which protects sending keys exchanged with one peer."""
		context = _WRAP_CONTEXT + b"".join(sorted((self.public_key, peer_public_key)))
		return key_agreement(
			static_priv=self._private_key,
			static_pub=import_x25519_public_key(peer_public_key),
			kdf=lambda shared: HKDF(shared, _KEY_LENGTH, b"", SHA256, context=context),
		)

	# Inbound bookkeeping

	def observe(self, obj):
		"""Look at a clear inbound message. Return True when it belonged to this protocol."""
		type = obj.get("type")
		with self._lock:
			if type == "channel_joined":
				self._on_channel_joined(obj.get("clients") or [])
			elif type == "client_joined":
				self._track(obj.get("client"))
			elif type == "client_left":
				self._forget(obj.get("client"))
			elif type == MSG_HELLO:
				self._on_hello(obj)
				return True
			elif type == MSG_KEY:
				self._on_key(obj)
				return True
		return False

	def _on_channel_joined(self, clients):
		self.reset()
		for client in clients:
			self._track(client)
		if self._peers:
			self._write(MSG_HELLO, pub=_b64(self.public_key))

	def _talks_to(self, client):
		"""The relay only forwards messages between the two sides of a channel."""
		if not isinstance(client, dict) or client.get("id") is None:
			return False
		mine = self._connection_type()
		return mine is None or client.get("connection_type") != mine

	def _track(self, client):
		if self._talks_to(client) and client["id"] not in self._peers:
			self._peers[client["id"]] = _Peer(client["id"], self._clock())
			self._last_state = None

	def _forget(self, client):
		if not isinstance(client, dict):
			return
		peer = self._peers.pop(client.get("id"), None)
		if peer is not None and peer.kid is not None:
			self._received_keys.pop(peer.kid, None)
		self._maybe_flush()

	def _peer_for(self, obj, public_key):
		"""Return the peer a greeting or a key came from, learning about it if needed."""
		origin = obj.get("origin")
		if origin is None:
			# Without an origin the relay leaves nothing to name the peer by: use its key.
			origin = "pub:" + _b64(public_key)
		peer = self._peers.get(origin)
		if peer is None:
			peer = _Peer(origin, self._clock())
			self._peers[origin] = peer
		if peer.public_key != public_key:
			peer.public_key = public_key
			peer.wrap_key = self._wrap_key_for(public_key)
			peer.key_sent = False
		return peer

	def _send_key_to(self, peer):
		# Marked before writing: the answer to this key may come back at once.
		peer.key_sent = True
		envelope = seal(peer.wrap_key, self.sending_key, self.public_key + peer.public_key)
		self._write(
			MSG_KEY,
			to=_b64(peer.public_key),
			pub=_b64(self.public_key),
			kid=self.kid,
			**envelope,
		)

	def _on_hello(self, obj):
		try:
			public_key = _unb64(obj["pub"])
			peer = self._peer_for(obj, public_key)
		except Exception:
			log.warning("Ignoring a malformed end-to-end greeting")
			return
		self._send_key_to(peer)
		self._maybe_flush()

	def _on_key(self, obj):
		try:
			if _unb64(obj["to"]) != self.public_key:
				return
			public_key = _unb64(obj["pub"])
			peer = self._peer_for(obj, public_key)
			plaintext = open_envelope(peer.wrap_key, obj, public_key + self.public_key)
			kid = obj["kid"]
		except Exception:
			log.warning("Ignoring a malformed end-to-end key message")
			return
		if plaintext is None or len(plaintext) != _KEY_LENGTH or not isinstance(kid, str):
			log.warning("Ignoring an end-to-end key which could not be unwrapped")
			return
		if peer.kid is not None:
			self._received_keys.pop(peer.kid, None)
		peer.kid = kid
		self._received_keys[kid] = plaintext
		if not peer.key_sent:
			self._send_key_to(peer)
		self._maybe_flush()

	# Outbound

	def state(self, now=None):
		"""Say what to do with a message to send right now.

		``ENCRYPTED`` when every peer holds our sending key, ``PENDING`` while a peer
		has still a chance to answer the greeting, ``CLEAR`` when there is nobody or
		when a peer never answered.
		"""
		with self._lock:
			if now is None:
				now = self._clock()
			result = ENCRYPTED if self._peers else CLEAR
			for peer in self._peers.values():
				if peer.key_sent:
					continue
				if now - peer.since >= self.timeout:
					result = CLEAR
					break
				result = PENDING
			self._log_state(result)
			return result

	@property
	def secure(self):
		"""True when what this client sends can only be read by its peers."""
		return self.state() == ENCRYPTED

	def _log_state(self, result):
		if result == PENDING or result == self._last_state:
			return
		self._last_state = result
		if result == ENCRYPTED:
			log.info("End-to-end encryption established with %d peer(s)", len(self._peers))
		elif self._peers:
			unanswered = [str(peer.id) for peer in self._peers.values() if not peer.key_sent]
			# An older TeleNVDA or the official NVDA Remote client: expected, not a fault.
			log.info(
				"Peer(s) %s do not support end-to-end encryption: the session stays readable by the relay",
				", ".join(unanswered),
			)

	def seal(self, plaintext):
		"""Seal an outgoing message with our sending key. Returns the ``encrypted`` fields."""
		envelope = seal(self.sending_key, plaintext)
		envelope["kid"] = self.kid
		return envelope

	def seal_for(self, target, plaintext):
		"""Seal a message meant for one peer, or return None when it cannot read us yet."""
		with self._lock:
			peer = self._peers.get(target)
			if peer is None or not peer.key_sent:
				return None
		return self.seal(plaintext)

	def open(self, obj):
		"""Return the plaintext of an ``encrypted`` message from a peer, or None."""
		kid = obj.get("kid")
		with self._lock:
			key = self._received_keys.get(kid)
		if key is None:
			log.debug("Dropping an encrypted message sealed with an unknown key")
			return None
		return open_envelope(key, obj)

	def expects_sealed(self, origin):
		"""True when messages from this peer should have been sealed."""
		with self._lock:
			peer = self._peers.get(origin)
			return peer is not None and peer.kid is not None

	# Messages held back during a greeting

	def defer(self, serialized):
		"""Hold a message back until the pending greetings are answered or time out."""
		with self._lock:
			self._deferred.append(serialized)
			if self._timer is None:
				self._timer = threading.Timer(self.timeout + 0.1, self.flush)
				self._timer.daemon = True
				self._timer.start()

	def _cancel_timer(self):
		if self._timer is not None:
			self._timer.cancel()
			self._timer = None

	def _maybe_flush(self):
		if self._deferred and self.state() != PENDING:
			self.flush()

	def flush(self):
		"""Hand the messages held back over to ``flush`` so that they are sent at last."""
		with self._lock:
			self._cancel_timer()
			items, self._deferred = self._deferred, []
		if items and self._flush is not None:
			self._flush(items)
