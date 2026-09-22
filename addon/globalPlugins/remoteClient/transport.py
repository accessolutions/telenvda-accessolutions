import threading
import os
import sys
import buildVersion
import time
import http.client
import queue
import ssl
import socket
import select
import hashlib
import base64
import json
from typing import Tuple
from urllib.parse import urlencode
from logging import getLogger
from . import callback_manager
from . import configuration
from . import https_connection, https_protocol, https_stream, https_version
from . import proxy_utils, sspi_proxy, ws_protocol
from .socket_utils import hostport_to_address
from enum import Enum

sys.path.append(
	os.path.join(
		os.path.abspath(os.path.dirname(__file__)),
		"lib64" if buildVersion.version_year >= 2026 else "lib32",
	),
)
from Cryptodome.Cipher import AES
import websocket

sys.path.remove(sys.path[-1])

log = getLogger("transport")

PROTOCOL_VERSION: int = 2
EXCLUDED_FROM_ENCRYPTION: list[str] = [
	"join",
	"protocol_version",
	"encrypted",
	"channel_joined",
	"motd",
	"nvda_not_connected",
	"client_left",
	"ping",
	"error",
	"client_joined",
	"generate_key",
]

#: Screen sharing signalling messages exchanged between two clients.
#: Unlike every other message, the relay does not broadcast them: it reads their
#: "type" and "target" fields to deliver them to a single peer. They can therefore
#: not be wrapped in an "encrypted" message. When an encryption password is in use,
#: only their payload is sealed, in a nested "enc" object, so that the relay keeps
#: routing them without ever seeing the session description or the ICE candidates.
SIGNALING_TYPES: frozenset = frozenset(
	(
		"screen_share_request",
		"screen_share_response",
		"screen_share_stop",
		"webrtc_offer",
		"webrtc_answer",
		"webrtc_candidate",
	),
)

#: Screen sharing messages exchanged with the relay itself rather than with a peer.
#: They carry no private data and the relay obviously has to read them, so they are
#: always sent in clear text.
SIGNALING_RELAY_TYPES: frozenset = frozenset(("capabilities", "turn_credentials"))

#: Fields of a signalling message which stay readable by the relay.
#: "target" tells it where to deliver the message and "origin" is stamped by it.
SIGNALING_CLEAR_FIELDS: Tuple[str, ...] = ("target", "origin")


class TransportEvents(Enum):
	CONNECTED = "transport_connected"
	CERTIFICATE_AUTHENTICATION_FAILED = "certificate_authentication_failed"
	CONNECTION_FAILED = "transport_connection_failed"
	CLOSING = "transport_closing"
	DISCONNECTED = "transport_disconnected"


class Transport:
	connected: bool
	successful_connects: int
	callback_manager: callback_manager.CallbackManager
	connect_event: threading.Event

	def __init__(self, serializer):
		self.serializer = serializer
		self.callback_manager = callback_manager.CallbackManager()
		self.connected = False
		self.successful_connects = 0
		self.connected_event = threading.Event()

	def transport_connected(self):
		self.successful_connects += 1
		self.connected = True
		self.connected_event.set()
		self.callback_manager.call_callbacks(TransportEvents.CONNECTED)


class TCPTransport(Transport):
	buffer: bytes
	closed: bool
	queue: queue.Queue
	insecure: bool
	server_sock_lock: threading.Lock

	def __init__(
		self,
		serializer,
		address: Tuple[str, int],
		timeout: int = 0,
		insecure: bool = False,
		encryption_key: str = "",
	):
		super().__init__(serializer=serializer)
		self.closed = False
		# Buffer to hold partially received data
		self.buffer = b""
		self.queue = queue.Queue()
		self.address = address
		self.server_sock = None
		# Reading/writing from an SSL socket is not thread safe.
		# See https://bugs.python.org/issue41597#msg375692
		# Guard access to the socket with a lock.
		self.server_sock_lock = threading.Lock()
		self.queue_thread = None
		self.timeout = timeout
		self.reconnector_thread = ConnectorThread(self)
		self.insecure = insecure
		self.encryption_key = encryption_key
		self.encryption_hash = (
			hashlib.sha256(encryption_key.encode("utf-8")).digest() if encryption_key else None
		)
		self.send_alpn = True

	def run(self):
		self.closed = False
		try:
			self.server_sock = self.create_outbound_socket(*self.address, insecure=self.insecure)
			self.server_sock.connect(self.address)
		except ssl.SSLCertVerificationError:
			fingerprint = None
			try:
				tmp_con = self.create_outbound_socket(*self.address, insecure=True)
				tmp_con.connect(self.address)
				certBin = tmp_con.getpeercert(True)
				tmp_con.close()
				fingerprint = hashlib.sha256(certBin).hexdigest().lower()
			except Exception:
				pass
			config = configuration.get_config()
			if (
				hostport_to_address(self.address) in config["trusted_certs"]
				and config["trusted_certs"][hostport_to_address(self.address)] == fingerprint
			):
				self.insecure = True
				return self.run()
			self.last_fail_fingerprint = fingerprint
			self.callback_manager.call_callbacks(TransportEvents.CERTIFICATE_AUTHENTICATION_FAILED)
			raise
		except ssl.SSLError:
			if self.send_alpn:
				self.send_alpn = False
				return self.run()
			else:
				self.callback_manager.call_callbacks(TransportEvents.CONNECTION_FAILED)
				raise
		except Exception:
			self.callback_manager.call_callbacks(TransportEvents.CONNECTION_FAILED)
			raise
		self.transport_connected()
		self.queue_thread = threading.Thread(target=self.send_queue)
		self.queue_thread.daemon = True
		self.queue_thread.start()
		while self.server_sock is not None:
			try:
				readers, writers, error = select.select([self.server_sock], [], [self.server_sock])
			except socket.error:
				self.buffer = b""
				break
			if self.server_sock in error:
				self.buffer = b""
				break
			if self.server_sock in readers:
				try:
					self.handle_server_data()
				except socket.error:
					self.buffer = b""
					break
		self.connected = False
		self.connected_event.clear()
		self.callback_manager.call_callbacks(TransportEvents.DISCONNECTED)
		self._disconnect()

	def create_outbound_socket(self, host, port, insecure=False):
		if host.lower().endswith(".onion"):
			server_sock = socket.socket(socket.AF_INET)
		else:
			address = socket.getaddrinfo(host, port)[0]
			server_sock = socket.socket(*address[:3])
		if self.timeout:
			server_sock.settimeout(self.timeout)
		server_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
		server_sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 60000, 2000))
		ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
		if self.send_alpn:
			ctx.set_alpn_protocols(["nvdaremote/2.0"])
		ctx.minimum_version = ssl.TLSVersion.TLSv1_2
		if insecure:
			ctx.check_hostname = False
			ctx.verify_mode = ssl.CERT_NONE
		ctx.load_default_certs()
		server_sock = ctx.wrap_socket(sock=server_sock, server_hostname=host)
		return server_sock

	def getpeercert(self, binary_form=False):
		if self.server_sock is None:
			return None
		return self.server_sock.getpeercert(binary_form)

	def handle_server_data(self):
		# This approach may be problematic:
		# See also server.py handle_data in class Client.
		buffSize = 16384
		with self.server_sock_lock:
			# select operates on the raw socket. Even though it said there was data to
			# read, that might be SSL data which might not result in actual data for
			# us. Therefore, do a non-blocking read so SSL doesn't try to wait for
			# more data for us.
			# We don't make the socket non-blocking earlier because then we'd have to
			# handle retries during the SSL handshake.
			# See https://stackoverflow.com/questions/3187565/select-and-ssl-in-python
			# and https://docs.python.org/3/library/ssl.html#notes-on-non-blocking-sockets
			self.server_sock.setblocking(False)
			try:
				data = self.buffer + self.server_sock.recv(buffSize)
			except ssl.SSLWantReadError:
				# There's no data for us.
				return
			finally:
				self.server_sock.setblocking(True)
		self.buffer = b""
		if not data:
			self._disconnect()
			return
		if b"\n" not in data:
			self.buffer += data
			return
		while b"\n" in data:
			line, sep, data = data.partition(b"\n")
			self.parse(line)
		self.buffer += data

	def parse(self, line, isDecrypted=False):
		obj = self.serializer.deserialize(line)
		if "type" not in obj:
			return
		if self.encryption_hash is not None and not isDecrypted:
			if obj["type"] in SIGNALING_TYPES:
				obj = self.open_signaling_envelope(obj)
				if obj is None:
					return
			elif obj["type"] not in EXCLUDED_FROM_ENCRYPTION and obj["type"] not in SIGNALING_RELAY_TYPES:
				return
		if obj["type"] == "encrypted" and self.encryption_hash is not None:
			cipher = AES.new(
				self.encryption_hash,
				AES.MODE_GCM,
				nonce=base64.b64decode(obj["nonce"].encode("utf-8")),
			)
			try:
				decrypted_data = cipher.decrypt_and_verify(
					base64.b64decode(obj["data"].encode("utf-8")),
					base64.b64decode(obj["tag"].encode("utf-8")),
				)
				return self.parse(decrypted_data, isDecrypted=True)
			except Exception:
				return
		callback = "msg_" + obj["type"]
		del obj["type"]
		self.callback_manager.call_callbacks(callback, **obj)

	def seal_signaling_envelope(self, kwargs):
		"""Encrypt the private part of a screen sharing signalling message.

		The routing fields stay readable so that the relay can deliver the message
		to the intended peer, everything else is sealed with the session password.
		"""
		envelope = {key: kwargs[key] for key in SIGNALING_CLEAR_FIELDS if key in kwargs}
		payload = {key: value for key, value in kwargs.items() if key not in SIGNALING_CLEAR_FIELDS}
		cipher = AES.new(self.encryption_hash, AES.MODE_GCM)
		data, tag = cipher.encrypt_and_digest(json.dumps(payload).encode("utf-8"))
		envelope["enc"] = {
			"nonce": base64.b64encode(cipher.nonce).decode(),
			"data": base64.b64encode(data).decode(),
			"tag": base64.b64encode(tag).decode(),
		}
		return envelope

	def open_signaling_envelope(self, obj):
		"""Return the signalling message with its payload decrypted, or None when invalid.

		A missing or unreadable payload means the message was not produced by a peer
		holding the session password, so it is dropped rather than acted upon.
		"""
		envelope = obj.pop("enc", None)
		if not isinstance(envelope, dict):
			log.warning("Dropping an unencrypted %s signalling message" % obj["type"])
			return None
		try:
			cipher = AES.new(
				self.encryption_hash,
				AES.MODE_GCM,
				nonce=base64.b64decode(envelope["nonce"].encode("utf-8")),
			)
			decrypted = cipher.decrypt_and_verify(
				base64.b64decode(envelope["data"].encode("utf-8")),
				base64.b64decode(envelope["tag"].encode("utf-8")),
			)
			payload = json.loads(decrypted.decode("utf-8"))
		except Exception:
			log.warning("Dropping a %s signalling message which could not be decrypted" % obj["type"])
			return None
		if not isinstance(payload, dict):
			return None
		# The routing fields are authoritative: they are the ones the relay acted upon.
		for key in ("type",) + SIGNALING_CLEAR_FIELDS:
			payload.pop(key, None)
		obj.update(payload)
		return obj

	def send_queue(self):
		while True:
			item = self.queue.get()
			if item is None:
				return
			try:
				with self.server_sock_lock:
					self.server_sock.sendall(item)
			except socket.error:
				return

	def send(self, type, **kwargs):
		if self.encryption_hash is not None and type in SIGNALING_TYPES:
			kwargs = self.seal_signaling_envelope(kwargs)
		obj = self.serializer.serialize(type=type, **kwargs)
		if (
			self.encryption_hash is not None
			and type not in EXCLUDED_FROM_ENCRYPTION
			and type not in SIGNALING_TYPES
			and type not in SIGNALING_RELAY_TYPES
		):
			cipher = AES.new(self.encryption_hash, AES.MODE_GCM)
			nonce = base64.b64encode(cipher.nonce).decode()
			data, tag = cipher.encrypt_and_digest(obj)
			data = base64.b64encode(data).decode()
			tag = base64.b64encode(tag).decode()
			return self.send(type="encrypted", nonce=nonce, data=data, tag=tag)
		self.write(obj)

	def write(self, obj):
		"""Hand a serialised message over to the wire.

		Subclasses carrying the stream over something else than a raw socket only have
		this to override, so that the encryption and signalling rules stay in one place.
		"""
		if self.connected:
			self.queue.put(obj)

	def _disconnect(self):
		"""Disconnect the transport due to an error, without closing the connector thread."""
		if self.queue_thread is not None:
			self.queue.put(None)
			self.queue_thread.join()
			self.queue_thread = None
		clear_queue(self.queue)
		if self.server_sock:
			self.server_sock.close()
			self.server_sock = None

	def close(self):
		self.callback_manager.call_callbacks(TransportEvents.CLOSING)
		self.reconnector_thread.running = False
		self._disconnect()
		self.closed = True
		self.reconnector_thread = ConnectorThread(self)


class RelayTransport(TCPTransport):
	def __init__(
		self,
		serializer,
		address,
		timeout=0,
		channel=None,
		connection_type=None,
		protocol_version=PROTOCOL_VERSION,
		insecure=False,
		encryption_key=None,
	):
		super().__init__(
			address=address,
			serializer=serializer,
			timeout=timeout,
			insecure=insecure,
			encryption_key=encryption_key,
		)
		log.info("Connecting to %s channel %s" % (address, channel))
		self.channel = channel
		self.connection_type = connection_type
		self.protocol_version = protocol_version
		self.callback_manager.register_callback(TransportEvents.CONNECTED, self.on_connected)

	def on_connected(self):
		self.send("protocol_version", version=self.protocol_version)
		if self.channel is not None:
			self.send("join", channel=self.channel, connection_type=self.connection_type)
		else:
			self.send("generate_key")


class WebSocketTransport(TCPTransport):
	"""Transport the NVDA Remote JSON stream in WebSocket text frames."""

	def __init__(
		self,
		serializer,
		address,
		ws_path="/",
		timeout=0,
		channel=None,
		connection_type=None,
		protocol_version=PROTOCOL_VERSION,
		insecure=False,
		encryption_key=None,
	):
		super().__init__(
			serializer=serializer,
			address=address,
			timeout=timeout,
			insecure=insecure,
			encryption_key=encryption_key,
		)
		self.ws_path = ws_protocol.normalize_path(ws_path)
		self.channel = channel
		self.connection_type = connection_type
		self.protocol_version = protocol_version
		self.websocket = None
		self._certificate_authentication_failed = False

	def _websocket_url(self):
		host, port = self.address
		return ws_protocol.websocket_url(host, port, self.ws_path)

	def _create_sspi_websocket(self, proxy_settings, insecure=None):
		"""Create a WebSocket over an HTTP CONNECT tunnel authenticated by SSPI."""
		if insecure is None:
			insecure = self.insecure
		host, port = self.address
		raw_socket = sspi_proxy.open_sspi_proxy_tunnel(
			proxy_settings,
			host,
			port,
			timeout=self.timeout or 60,
		)
		tls_socket = None
		try:
			context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
			context.minimum_version = ssl.TLSVersion.TLSv1_2
			if insecure:
				context.check_hostname = False
				context.verify_mode = ssl.CERT_NONE
			else:
				context.load_default_certs()
				context.check_hostname = True
				context.verify_mode = ssl.CERT_REQUIRED
			tls_socket = context.wrap_socket(raw_socket, server_hostname=host)
			raw_socket = None
			return websocket.create_connection(
				self._websocket_url(),
				socket=tls_socket,
				subprotocols=[ws_protocol.SUBPROTOCOL],
				sslopt=self._ssl_options(insecure),
				timeout=self.timeout or None,
			)
		except Exception:
			if tls_socket is not None:
				tls_socket.close()
			elif raw_socket is not None:
				raw_socket.close()
			raise

	def _ssl_options(self, insecure):
		"""Build the websocket-client TLS options.

		``check_hostname`` must be disabled explicitly along with ``cert_reqs``,
		otherwise the SSL context refuses the combination and the connection
		fails before the handshake.
		"""
		if insecure:
			return {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
		return {"cert_reqs": ssl.CERT_REQUIRED}

	def _open_websocket(self, proxy_settings, insecure):
		"""Open the WebSocket through the resolved proxy, if any."""
		if proxy_utils.uses_sspi(proxy_settings):
			return self._create_sspi_websocket(proxy_settings, insecure=insecure)
		kwargs = {
			"subprotocols": [ws_protocol.SUBPROTOCOL],
			"sslopt": self._ssl_options(insecure),
			"timeout": self.timeout or None,
		}
		kwargs.update(proxy_utils.websocket_options(proxy_settings))
		try:
			return websocket.create_connection(self._websocket_url(), **kwargs)
		except Exception as error:
			if not proxy_utils.needs_windows_authentication(proxy_settings, error):
				raise
			log.debug("The proxy requires authentication, retrying with Windows credentials")
			return self._create_sspi_websocket(
				proxy_utils.with_windows_authentication(proxy_settings),
				insecure=insecure,
			)

	def create_websocket(self):
		self._certificate_authentication_failed = False
		conf = configuration.get_config().get("controlserver", {})
		url = self._websocket_url()
		proxy_settings = proxy_utils.resolve_for_url(proxy_utils.from_config(conf), url)
		log.debug(
			"Opening %s (proxy %s)",
			url,
			f"{proxy_settings.type}://{proxy_settings.host}:{proxy_settings.port}"
			if proxy_settings.enabled
			else "none",
		)
		try:
			return self._open_websocket(proxy_settings, self.insecure)
		except Exception as error:
			ssl_error = error if isinstance(error, ssl.SSLError) else error.__cause__
			if self.insecure or not isinstance(ssl_error, ssl.SSLError):
				raise
			fingerprint = None
			try:
				probe = self._open_websocket(proxy_settings, True)
				certificate_socket = getattr(probe, "sock", None)
				if certificate_socket is not None and not isinstance(certificate_socket, ssl.SSLSocket):
					certificate_socket = getattr(certificate_socket, "sock", None)
				if certificate_socket:
					fingerprint = hashlib.sha256(certificate_socket.getpeercert(True)).hexdigest().lower()
				probe.close()
			except Exception:
				log.exception("Unable to read the WebSocket certificate fingerprint")
			trusted = (
				configuration.get_config().get("trusted_certs", {}).get(hostport_to_address(self.address))
			)
			if fingerprint and trusted == fingerprint:
				self.insecure = True
				return self.create_websocket()
			self.last_fail_fingerprint = fingerprint
			self._certificate_authentication_failed = True
			self.callback_manager.call_callbacks(TransportEvents.CERTIFICATE_AUTHENTICATION_FAILED)
			raise

	def run(self):
		self.closed = False
		try:
			self.websocket = self.create_websocket()
		except ssl.SSLCertVerificationError:
			if not self._certificate_authentication_failed:
				self.callback_manager.call_callbacks(TransportEvents.CERTIFICATE_AUTHENTICATION_FAILED)
			raise
		except Exception:
			if not self._certificate_authentication_failed:
				self.callback_manager.call_callbacks(TransportEvents.CONNECTION_FAILED)
			raise
		self.transport_connected()
		while self.websocket is not None and not self.closed:
			try:
				data = self.websocket.recv()
			except Exception:
				break
			if data in (None, "", b""):
				break
			if isinstance(data, str):
				data = data.encode("utf-8")
			for line in data.splitlines():
				if line:
					self.parse(line)
		self.connected = False
		self.connected_event.clear()
		self.callback_manager.call_callbacks(TransportEvents.DISCONNECTED)
		self._disconnect()

	def write(self, obj):
		if self.connected and self.websocket is not None:
			self.websocket.send(obj.decode("utf-8"))

	def _disconnect(self):
		if self.websocket is not None:
			try:
				self.websocket.close()
			except Exception:
				pass
			self.websocket = None

	def close(self):
		self.callback_manager.call_callbacks(TransportEvents.CLOSING)
		self.reconnector_thread.running = False
		self.closed = True
		self._disconnect()
		self.connected = False
		self.reconnector_thread = ConnectorThread(self)


class _HTTPSRequestError(Exception):
	def __init__(self, status, message):
		super().__init__(message)
		self.status = status


class _HTTPSVersionUnavailable(Exception):
	"""The requested HTTPS relay version cannot be used on this server or network."""


class HTTPSTransport(TCPTransport):
	"""Transport TeleNVDA over an HTTPS long-polling session."""

	PROTOCOL = https_protocol.PROTOCOL
	BASE_PATH = https_protocol.DEFAULT_PATH
	TRANSPORT_HEADER = "https-long-poll/1"

	def __init__(
		self,
		serializer,
		address,
		https_path=None,
		timeout=0,
		channel=None,
		connection_type=None,
		protocol_version=PROTOCOL_VERSION,
		insecure=False,
		encryption_key=None,
		poll_wait=25,
	):
		super().__init__(
			serializer=serializer,
			address=address,
			timeout=timeout,
			insecure=insecure,
			encryption_key=encryption_key,
		)
		self.https_path = https_protocol.normalize_https_path(
			https_path if https_path is not None else self.BASE_PATH,
		)
		self.channel = channel
		self.connection_type = connection_type
		self.protocol_version = protocol_version
		self.poll_wait = max(1, int(poll_wait))
		self.session_id = None
		self.session_token = None
		self.next_send_sequence = 1
		self.last_received_sequence = 0
		self.max_batch_messages = https_protocol.MAX_BATCH_MESSAGES
		self.max_message_bytes = 1 << 20
		self.stream_keepalive = https_protocol.DEFAULT_STREAM_KEEPALIVE
		self.acknowledged_sequence = 0
		self.ack_interval = 10
		self.send_connection = None
		self.poll_connection = None
		self._send_thread = None
		self._stop_event = threading.Event()
		self._state_lock = threading.Lock()
		self._connections_lock = threading.Lock()
		self._send_request_lock = threading.Lock()
		self._poll_request_lock = threading.Lock()
		self.queue = queue.Queue(maxsize=256)
		self._proxy_settings = None
		self._certificate_authentication_failed = False
		self.last_fail_fingerprint = None

	def _reset_http_state(self):
		self._stop_event.clear()
		self.connected = False
		self.connected_event.clear()
		self._certificate_authentication_failed = False
		self.last_fail_fingerprint = None
		self.session_id = None
		self.session_token = None
		self.next_send_sequence = 1
		self.last_received_sequence = 0
		self.max_batch_messages = https_protocol.MAX_BATCH_MESSAGES
		self.max_message_bytes = 1 << 20
		self.stream_keepalive = https_protocol.DEFAULT_STREAM_KEEPALIVE
		self.acknowledged_sequence = 0
		self._proxy_settings = None
		clear_queue(self.queue)

	def _relay_url(self):
		host, port = self.address
		return https_protocol.https_url(host, port, self.https_path)

	def _resolve_proxy(self):
		config = configuration.get_config()
		proxy_settings = proxy_utils.from_config(config)
		return proxy_utils.resolve_for_url(proxy_settings, self._relay_url())

	def _open_http_connection(self):
		return https_connection.open_https_connection(
			self.address,
			self._proxy_settings,
			self.timeout,
			insecure=self.insecure,
		)

	def _open_http_connection_with_certificate_policy(self):
		self._certificate_authentication_failed = False
		try:
			return self._open_http_connection()
		except Exception as error:
			if self.insecure or not https_connection.is_certificate_verification_error(error):
				raise
			fingerprint = None
			try:
				probe = https_connection.open_https_connection(
					self.address,
					self._proxy_settings,
					self.timeout,
					insecure=True,
				)
				try:
					fingerprint = https_connection.peer_certificate_fingerprint(probe)
				finally:
					probe.close()
			except Exception:
				log.exception("Unable to read the HTTPS certificate fingerprint")
			trusted = (
				configuration.get_config()
				.get("trusted_certs", {})
				.get(
					hostport_to_address(self.address),
				)
			)
			if fingerprint and trusted == fingerprint:
				self.insecure = True
				return self._open_http_connection()
			self.last_fail_fingerprint = fingerprint
			self._certificate_authentication_failed = True
			self.callback_manager.call_callbacks(TransportEvents.CERTIFICATE_AUTHENTICATION_FAILED)
			raise

	def _set_connection(self, name, connection):
		with self._connections_lock:
			setattr(self, name, connection)

	def _get_connection(self, name):
		with self._connections_lock:
			return getattr(self, name)

	def _close_http_connections(self):
		with self._connections_lock:
			connections = (self.send_connection, self.poll_connection)
			self.send_connection = None
			self.poll_connection = None
		for connection in connections:
			if connection is not None:
				try:
					connection.close()
				except Exception:
					pass

	def _request(self, connection_name, method, path, body=None, timeout=None):
		connection = self._get_connection(connection_name)
		if connection is None:
			raise OSError("HTTPS connection is not available")
		request_lock = (
			self._send_request_lock if connection_name == "send_connection" else self._poll_request_lock
		)
		headers = {
			"Accept": "application/json",
			"User-Agent": "TeleNVDA/%s" % getattr(buildVersion, "version_year", "unknown"),
			"X-TeleNVDA-Transport": self.TRANSPORT_HEADER,
		}
		if body is not None:
			headers["Content-Type"] = "application/json"
		if self.session_token is not None:
			headers["Authorization"] = "Bearer %s" % self.session_token
		old_timeout = None
		with request_lock:
			try:
				if timeout is not None and getattr(connection, "sock", None) is not None:
					old_timeout = connection.sock.gettimeout()
					connection.sock.settimeout(timeout)
				connection.request(method, path, body=body, headers=headers)
				response = connection.getresponse()
				data = response.read()
				response_headers = {key.lower(): value for key, value in response.getheaders()}
				return response.status, response_headers, data
			finally:
				if old_timeout is not None and getattr(connection, "sock", None) is not None:
					try:
						connection.sock.settimeout(old_timeout)
					except Exception:
						pass

	def _create_session(self):
		status, _, body = self._request(
			"send_connection",
			"POST",
			https_protocol.sessions_path(self.https_path),
			https_protocol.encode_session_request(self.PROTOCOL),
		)
		if status == 404 and self.PROTOCOL == https_protocol.PROTOCOL_V2:
			raise _HTTPSVersionUnavailable("The server does not expose the HTTPS v2 relay")
		if status != 201:
			raise _HTTPSRequestError(status, "HTTPS session creation failed with status %s" % status)
		response = https_protocol.decode_session_response(body)
		if response.protocol != self.PROTOCOL:
			raise https_protocol.HTTPSProtocolError(
				"HTTPS relay returned an unexpected protocol",
				reason="unsupported_protocol",
			)
		self.session_id = response.session_id
		self.session_token = response.token
		self.poll_wait = response.poll_wait_seconds
		self.max_batch_messages = min(response.max_batch_messages, https_protocol.MAX_BATCH_MESSAGES)
		self.max_message_bytes = response.max_message_bytes
		self.stream_keepalive = response.stream_keepalive_seconds

	def _message_payload(self, serialized):
		try:
			payload = json.loads(serialized)
		except (TypeError, ValueError) as error:
			raise https_protocol.HTTPSProtocolError("Serialized message is not valid JSON") from error
		if not isinstance(payload, dict):
			raise https_protocol.HTTPSProtocolError("Serialized message must be a JSON object")
		return payload

	def _take_batch(self):
		try:
			serialized = self.queue.get(timeout=0.2)
		except queue.Empty:
			return []
		items = []
		while serialized is not None and len(items) < self.max_batch_messages:
			try:
				payload = self._message_payload(serialized)
			except https_protocol.HTTPSProtocolError:
				log.warning("Dropping an invalid serialized HTTPS message")
			else:
				payload_size = len(
					json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
				)
				if payload_size > self.max_message_bytes:
					log.warning("Dropping an oversized HTTPS message")
				else:
					with self._state_lock:
						sequence = self.next_send_sequence
						self.next_send_sequence += 1
					items.append(https_protocol.MessageEnvelope(sequence, payload))
			try:
				serialized = self.queue.get_nowait()
			except queue.Empty:
				break
		return items

	def _reopen_send_connection(self):
		old_connection = self._get_connection("send_connection")
		if old_connection is not None:
			try:
				old_connection.close()
			except Exception:
				pass
		self._set_connection("send_connection", self._open_http_connection())

	def _send_batch(self, batch):
		failures = 0
		while batch and not self._stop_event.is_set():
			try:
				received_through = self.last_received_sequence
				body = https_protocol.encode_message_batch(batch, received_through)
				status, headers, response_body = self._request(
					"send_connection",
					"POST",
					https_protocol.messages_path(self.https_path, self.session_id),
					body,
				)
			except (
				OSError,
				ValueError,
				http.client.HTTPException,
				https_protocol.HTTPSProtocolError,
			) as error:
				failures += 1
				if failures >= 3:
					log.warning("HTTPS message delivery failed: %s", type(error).__name__)
					self._stop_event.set()
					return
				try:
					self._reopen_send_connection()
				except Exception:
					pass
				if self._stop_event.wait(min(failures, 3)):
					return
				continue
			if status in (429, 503):
				failures += 1
				if failures >= 3:
					log.warning("HTTPS message delivery returned temporary status %s", status)
					self._stop_event.set()
					return
				if self._stop_event.wait(self._retry_delay(headers, failures)):
					return
				continue
			if status in (401, 404, 410):
				log.info("HTTPS session is no longer available (status %s)", status)
				self._stop_event.set()
				return
			if status != 200:
				log.warning("HTTPS message delivery returned status %s", status)
				self._stop_event.set()
				return
			self.acknowledged_sequence = max(self.acknowledged_sequence, received_through)
			try:
				accepted_through = https_protocol.decode_ack_response(response_body)
			except https_protocol.HTTPSProtocolError:
				self._stop_event.set()
				return
			if accepted_through < batch[0].sequence - 1 or accepted_through > batch[-1].sequence:
				self._stop_event.set()
				return
			batch = [message for message in batch if message.sequence > accepted_through]

	def _send_acknowledgement(self):
		received = self.last_received_sequence
		try:
			status, _, _ = self._request(
				"send_connection",
				"POST",
				https_protocol.messages_path(self.https_path, self.session_id),
				https_protocol.encode_message_batch([], received),
			)
		except (OSError, ValueError, http.client.HTTPException, https_protocol.HTTPSProtocolError):
			# A listening session may never send a batch, so this is the only
			# chance to heal the send connection before the next acknowledgement.
			try:
				self._reopen_send_connection()
			except Exception:
				pass
			return
		if status == 200:
			self.acknowledged_sequence = max(self.acknowledged_sequence, received)

	def _poll_messages(self):
		path = https_protocol.messages_path(self.https_path, self.session_id)
		query = urlencode({"after": self.last_received_sequence, "wait": self.poll_wait})
		return self._request("poll_connection", "GET", "%s?%s" % (path, query))

	def _retry_delay(self, headers, failure_count):
		value = headers.get("retry-after", "")
		try:
			return min(30, max(0, int(value)))
		except (TypeError, ValueError):
			return min(5, max(1, failure_count))

	def _send_loop(self):
		last_ack = 0.0
		while not self._stop_event.is_set():
			batch = self._take_batch()
			if batch:
				self._send_batch(batch)
				last_ack = time.monotonic()
				continue
			now = time.monotonic()
			if (
				self.last_received_sequence > self.acknowledged_sequence
				and now - last_ack >= self.ack_interval
			):
				self._send_acknowledgement()
				last_ack = now

	def run(self):
		self.closed = False
		self._reset_http_state()
		was_connected = False
		try:
			self._proxy_settings = self._resolve_proxy()
			self._set_connection("send_connection", self._open_http_connection_with_certificate_policy())
			self._create_session()
			self._set_connection("poll_connection", self._open_http_connection_with_certificate_policy())
			self.transport_connected()
			was_connected = True
			self._send_thread = threading.Thread(
				target=self._send_loop,
				name="TeleNVDA HTTPS sender",
				daemon=True,
			)
			self._send_thread.start()
			tracker = https_protocol.SequenceTracker()
			while not self._stop_event.is_set() and not self.closed:
				status, headers, body = self._poll_messages()
				if status in (429, 503):
					if self._stop_event.wait(self._retry_delay(headers, 1)):
						break
					continue
				if status in (401, 404, 410):
					raise _HTTPSRequestError(status, "HTTPS session is no longer available")
				if status != 200:
					raise _HTTPSRequestError(status, "HTTPS polling failed with status %s" % status)
				poll = https_protocol.decode_poll_response(body)
				for message in poll.messages:
					if tracker.accept(message.sequence):
						serialized = json.dumps(message.payload, ensure_ascii=True).encode("utf-8")
						self.parse(serialized)
				self.last_received_sequence = tracker.last_sequence
		except Exception:
			if not was_connected and not self._certificate_authentication_failed:
				self.callback_manager.call_callbacks(TransportEvents.CONNECTION_FAILED)
			raise
		finally:
			if was_connected:
				self.connected = False
				self.connected_event.clear()
				self.callback_manager.call_callbacks(TransportEvents.DISCONNECTED)
			self._disconnect()

	def write(self, obj):
		if not self.connected or self._stop_event.is_set():
			return
		try:
			self.queue.put_nowait(obj)
		except queue.Full:
			log.warning("HTTPS outgoing message queue is full")

	def _disconnect(self):
		self._stop_event.set()
		session_id = self.session_id
		if session_id is not None and self._get_connection("send_connection") is not None:
			try:
				self._request(
					"send_connection",
					"DELETE",
					https_protocol.session_path(self.https_path, session_id),
					timeout=2,
				)
			except Exception:
				pass
		self._close_http_connections()
		if self._send_thread is not None and self._send_thread is not threading.current_thread():
			self._send_thread.join(timeout=2)
		self._send_thread = None
		clear_queue(self.queue)
		with self._state_lock:
			self.session_id = None
			self.session_token = None
			self.next_send_sequence = 1
			self.last_received_sequence = 0
		self.connected = False

	def close(self):
		self.callback_manager.call_callbacks(TransportEvents.CLOSING)
		self.reconnector_thread.running = False
		self.closed = True
		self._disconnect()
		self.reconnector_thread = ConnectorThread(self)


class HTTPSStreamTransport(HTTPSTransport):
	"""Transport TeleNVDA over a single streaming HTTPS response."""

	PROTOCOL = https_protocol.PROTOCOL_V2
	BASE_PATH = https_protocol.DEFAULT_PATH_V2
	TRANSPORT_HEADER = "https-stream/2"
	READY_TIMEOUT = 3

	def _open_stream(self):
		connection = self._get_connection("poll_connection")
		if connection is None:
			raise OSError("HTTPS stream connection is not available")
		path = https_protocol.stream_path(self.https_path, self.session_id)
		query = urlencode({"after": self.last_received_sequence})
		headers = {
			"Accept": "text/event-stream",
			"User-Agent": "TeleNVDA/%s" % getattr(buildVersion, "version_year", "unknown"),
			"X-TeleNVDA-Transport": self.TRANSPORT_HEADER,
			"Authorization": "Bearer %s" % self.session_token,
		}
		if connection.sock is not None:
			connection.sock.settimeout(self.READY_TIMEOUT)
		connection.request("GET", "%s?%s" % (path, query), headers=headers)
		response = connection.getresponse()
		if response.status == 404:
			raise _HTTPSVersionUnavailable("The server does not expose the HTTPS stream")
		if response.status != 200:
			raise _HTTPSRequestError(response.status, "HTTPS stream failed with status %s" % response.status)
		content_type = response.getheader("Content-Type", "")
		if "text/event-stream" not in content_type.lower():
			raise _HTTPSVersionUnavailable("The stream response is not an event stream")
		return response

	def _await_ready(self, response):
		"""Consume blocks until ready proves that the stream is not buffered."""
		while True:
			event = https_stream.read_event(response, self.max_message_bytes)
			if event is None:
				continue
			if event.event == "ready":
				return
			raise _HTTPSVersionUnavailable("The stream did not start with a ready event")

	def fall_back_to_v1(self):
		"""Switch an automatic streaming connection to long polling."""
		if not getattr(self, "_https_fallback_enabled", False):
			return False
		key = https_version.network_key(self.address, self._proxy_settings)
		https_version.remember_failure(key)
		self.__class__ = HTTPSRelayTransport
		self.PROTOCOL = https_protocol.PROTOCOL
		self.BASE_PATH = https_protocol.DEFAULT_PATH
		self.TRANSPORT_HEADER = HTTPSRelayTransport.TRANSPORT_HEADER
		self.https_path = getattr(self, "_https_fallback_path", https_protocol.DEFAULT_PATH)
		return True

	def run(self):
		self.closed = False
		self._reset_http_state()
		was_connected = False
		response = None
		try:
			self._proxy_settings = self._resolve_proxy()
			self._set_connection("send_connection", self._open_http_connection_with_certificate_policy())
			self._create_session()
			self._set_connection("poll_connection", self._open_http_connection_with_certificate_policy())
			response = self._open_stream()
			try:
				self._await_ready(response)
			except socket.timeout as error:
				raise _HTTPSVersionUnavailable("The stream is buffered by an intermediary") from error
			connection = self._get_connection("poll_connection")
			if connection is not None and connection.sock is not None:
				connection.sock.settimeout(self.stream_keepalive * 2 + 5)
			https_version.remember_success(
				https_version.network_key(self.address, self._proxy_settings),
			)
			self.transport_connected()
			was_connected = True
			self._send_thread = threading.Thread(
				target=self._send_loop,
				name="TeleNVDA HTTPS sender",
				daemon=True,
			)
			self._send_thread.start()
			tracker = https_protocol.SequenceTracker()
			while not self._stop_event.is_set() and not self.closed:
				event = https_stream.read_event(response, self.max_message_bytes)
				if event is None:
					continue
				if event.event == "ready":
					continue
				if event.sequence is None:
					raise https_protocol.HTTPSProtocolError(
						"Stream event has no sequence",
						reason="invalid_sequence",
					)
				payload = https_stream.decode_event_payload(event)
				if tracker.accept(event.sequence):
					serialized = json.dumps(payload, ensure_ascii=True).encode("utf-8")
					self.parse(serialized)
				self.last_received_sequence = tracker.last_sequence
		except _HTTPSVersionUnavailable:
			raise
		except Exception:
			if not was_connected and not self._certificate_authentication_failed:
				self.callback_manager.call_callbacks(TransportEvents.CONNECTION_FAILED)
			raise
		finally:
			if response is not None:
				try:
					response.close()
				except Exception:
					pass
			if was_connected:
				self.connected = False
				self.connected_event.clear()
				self.callback_manager.call_callbacks(TransportEvents.DISCONNECTED)
			self._disconnect()


class RelayMixin:
	def on_connected(self):
		self.send("protocol_version", version=self.protocol_version)
		if self.channel is not None:
			self.send("join", channel=self.channel, connection_type=self.connection_type)
		else:
			self.send("generate_key")


class WebSocketRelayTransport(WebSocketTransport, RelayMixin):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.callback_manager.register_callback(TransportEvents.CONNECTED, self.on_connected)


class HTTPSRelayTransport(HTTPSTransport, RelayMixin):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.callback_manager.register_callback(TransportEvents.CONNECTED, self.on_connected)


class HTTPSStreamRelayTransport(HTTPSStreamTransport, RelayMixin):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.callback_manager.register_callback(TransportEvents.CONNECTED, self.on_connected)


def create_https_relay_transport(address, preference=https_version.AUTO, **kwargs):
	"""Create the HTTPS relay transport selected for this server and proxy route."""
	v1_path = kwargs.pop("https_path", https_protocol.DEFAULT_PATH)
	v2_path = kwargs.pop("https_path_v2", https_protocol.DEFAULT_PATH_V2)
	proxy_settings = proxy_utils.resolve_for_url(
		proxy_utils.from_config(configuration.get_config()),
		https_protocol.https_url(address[0], address[1], v2_path),
	)
	key = https_version.network_key(address, proxy_settings)
	versions = https_version.versions_to_try(preference, key)
	if versions[0] == https_version.V2:
		transport = HTTPSStreamRelayTransport(address=address, https_path=v2_path, **kwargs)
		transport._https_version_key = key
		transport._https_version_preference = preference
		transport._https_path_v1 = v1_path
		transport._https_path_v2 = v2_path
		transport._https_fallback_path = v1_path
		transport._https_fallback_enabled = preference == https_version.AUTO
		return transport
	transport = HTTPSRelayTransport(address=address, https_path=v1_path, **kwargs)
	transport._https_version_preference = preference
	transport._https_path_v1 = v1_path
	transport._https_path_v2 = v2_path
	return transport


class ConnectorThread(threading.Thread):
	def __init__(self, connector, connect_delay=5):
		super().__init__()
		self.connect_delay = connect_delay
		self.running = True
		self.connector = connector
		self.name = self.name + "_connector_loop"
		self.daemon = True

	def run(self):
		while self.running:
			try:
				self.connector.run()
			except _HTTPSVersionUnavailable:
				if not self.running:
					break
				if self.connector.fall_back_to_v1():
					continue
				time.sleep(self.connect_delay)
				continue
			except Exception:
				if not self.running:
					break
				time.sleep(self.connect_delay)
				continue
			else:
				time.sleep(self.connect_delay)
		log.info("Ending control connector thread %s" % self.name)


def clear_queue(queue):
	try:
		while True:
			queue.get_nowait()
	except Exception:
		pass
