"""HTTP/1.1 TLS connections used by the HTTPS long-polling transport."""

import base64
import hashlib
import http.client
import socket
import ssl
from typing import Callable

try:
	from . import proxy_tunnel
except ImportError:
	import proxy_tunnel


_DEFAULT_TIMEOUT = 60


class HTTPSConnectionError(OSError):
	"""Raised when a direct or proxy connection cannot be established."""


class HTTPSProxyError(HTTPSConnectionError):
	def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
		super().__init__(message)
		self.status = status
		self.headers = headers or {}


class HTTPSProxyAuthenticationError(HTTPSProxyError):
	def __init__(self, status: int, headers: dict[str, str] | None = None):
		super().__init__(status, "The HTTP proxy requires authentication", headers)


def open_https_connection(address, proxy_settings, timeout, insecure=False):
	"""Return a connected HTTP/1.1 TLS connection for one relay session."""
	host, port = _normalize_address(address)
	proxy_settings = _resolve_proxy_settings(proxy_settings, host, port)
	proxy_enabled = bool(
		getattr(proxy_settings, "enabled", False)
		or (getattr(proxy_settings, "host", "") and getattr(proxy_settings, "port", 0)),
	)
	timeout_value = timeout or _DEFAULT_TIMEOUT
	if not proxy_enabled:
		raw_socket = socket.create_connection((host, port), timeout_value)
	else:
		try:
			raw_socket = _open_proxy_socket(proxy_settings, host, port, timeout_value)
		except HTTPSProxyAuthenticationError as proxy_error:
			proxy_utils = _proxy_utils()
			if not proxy_utils.allows_windows_authentication(proxy_settings):
				raise
			try:
				raw_socket = _open_sspi_tunnel(
					proxy_utils.with_windows_authentication(proxy_settings),
					host,
					port,
					timeout_value,
				)
			except Exception as error:
				raise proxy_error from error
	try:
		context = proxy_tunnel.create_tls_context(insecure)
		tls_socket = context.wrap_socket(raw_socket, server_hostname=host)
	except Exception:
		raw_socket.close()
		raise
	connection = http.client.HTTPSConnection(host, port, timeout=timeout_value, context=context)
	# The socket is supplied already tunnelled and wrapped. Automatic reopening
	# would instead dial the target host directly, bypassing the proxy and the
	# SSPI tunnel, so the caller must open a fresh connection explicitly.
	connection.auto_open = 0
	connection.sock = tls_socket
	return connection


def peer_certificate_fingerprint(connection):
	"""Return the lowercase SHA-256 fingerprint of the peer certificate."""
	socket_object = getattr(connection, "sock", None)
	if socket_object is not None and not hasattr(socket_object, "getpeercert"):
		socket_object = getattr(socket_object, "sock", None)
	if socket_object is None:
		return None
	certificate = socket_object.getpeercert(True)
	if not certificate:
		return None
	return hashlib.sha256(certificate).hexdigest().lower()


def is_certificate_verification_error(error):
	"""Return whether an exception specifically indicates certificate rejection."""
	if isinstance(error, ssl.SSLCertVerificationError):
		return True
	cause = getattr(error, "__cause__", None)
	return cause is not None and isinstance(cause, ssl.SSLCertVerificationError)


def _proxy_utils():
	"""Return the proxy helpers, whether the module runs inside the add-on or alone."""
	try:
		from . import proxy_utils
	except ImportError:
		import proxy_utils
	return proxy_utils


def _resolve_proxy_settings(proxy_settings, host: str, port: int):
	if not getattr(proxy_settings, "auto_detect", False):
		return proxy_settings
	url = f"https://{proxy_tunnel.format_authority(host, port)}/"
	return _proxy_utils().resolve_for_url(proxy_settings, url)


def _normalize_address(address):
	if not isinstance(address, (tuple, list)) or len(address) != 2:
		raise HTTPSConnectionError("HTTPS address must contain a host and port")
	host, port = address
	if not isinstance(host, str) or not host:
		raise HTTPSConnectionError("HTTPS host is invalid")
	if host.startswith("[") and host.endswith("]"):
		host = host[1:-1]
	if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
		raise HTTPSConnectionError("HTTPS port is invalid")
	return host, port


def _open_proxy_socket(proxy_settings, host, port, timeout):
	proxy_type = str(getattr(proxy_settings, "type", "http")).lower()
	if proxy_type == "http":
		return _open_http_connect_tunnel(proxy_settings, host, port, timeout)
	if proxy_type in proxy_tunnel.SOCKS_TYPES:
		return _open_socks_tunnel(proxy_settings, host, port, timeout)
	if proxy_type in ("negotiate", "ntlm"):
		return _open_sspi_tunnel(proxy_settings, host, port, timeout)
	raise HTTPSConnectionError(f"Unsupported HTTPS proxy type: {proxy_type}")


def _open_http_connect_tunnel(
	proxy_settings,
	target_host: str,
	target_port: int,
	timeout: float,
	socket_factory: Callable[..., socket.socket] | None = None,
):
	proxy_host = getattr(proxy_settings, "host", "")
	proxy_port = getattr(proxy_settings, "port", 0)
	if not proxy_host or not proxy_port:
		raise HTTPSConnectionError("The HTTP proxy host and port must be configured")
	socket_factory = socket_factory or socket.create_connection
	proxy_socket = socket_factory((proxy_host, int(proxy_port)), timeout)
	authorization = ""
	username = getattr(proxy_settings, "username", "")
	if username:
		credentials = f"{username}:{getattr(proxy_settings, 'password', '')}".encode("utf-8")
		authorization = f"Basic {base64.b64encode(credentials).decode('ascii')}"
	try:
		proxy_socket.sendall(
			proxy_tunnel.build_connect_request(target_host, target_port, authorization),
		)
		status, headers = proxy_tunnel.read_connect_response(proxy_socket, HTTPSConnectionError)
		if status == 200:
			return proxy_socket
		if status == 407:
			raise HTTPSProxyAuthenticationError(status, headers)
		raise HTTPSProxyError(status, f"The HTTP proxy rejected CONNECT with status {status}", headers)
	except Exception:
		proxy_socket.close()
		raise


def _open_socks_tunnel(proxy_settings, target_host: str, target_port: int, timeout: float):
	try:
		return proxy_tunnel.open_socks_socket(proxy_settings, target_host, target_port, timeout)
	except Exception as error:
		raise HTTPSConnectionError("The SOCKS proxy connection failed") from error


def _open_sspi_tunnel(proxy_settings, target_host: str, target_port: int, timeout: float):
	try:
		from . import sspi_proxy
	except ImportError:
		import sspi_proxy
	try:
		return sspi_proxy.open_sspi_proxy_tunnel(
			proxy_settings,
			target_host,
			target_port,
			timeout=timeout,
		)
	except Exception as error:
		raise HTTPSConnectionError("The Windows proxy authentication failed") from error
