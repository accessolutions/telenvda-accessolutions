"""Socket level proxy tunnelling shared by the relay transports and the updater.

The WebSocket transport, the HTTPS relay transport and the updater all have to
reach the same server through the same proxy.  The code that speaks to the
proxy itself lives here so that a CONNECT exchange, a SOCKS handshake and the
TLS wrapping of the resulting socket behave identically everywhere.
"""

from pathlib import Path
import ssl
import sys


MAX_HEADER_BYTES = 65536
SOCKS_TYPES = ("socks4", "socks4a", "socks5", "socks5h")

_BODY_CHUNK_BYTES = 4096


class ProxyTunnelError(OSError):
	"""Raised when a proxy refuses or mishandles a tunnel request."""


def format_authority(host, port):
	"""Return the ``host:port`` authority, bracketing a bare IPv6 address."""
	if ":" in host and not host.startswith("["):
		host = f"[{host}]"
	return f"{host}:{int(port)}"


def build_connect_request(target_host, target_port, authorization=""):
	"""Return the bytes of one CONNECT request for the target host and port."""
	authority = format_authority(target_host, target_port)
	lines = [
		f"CONNECT {authority} HTTP/1.1",
		f"Host: {authority}",
		"Proxy-Connection: Keep-Alive",
		"Connection: Keep-Alive",
		"User-Agent: TeleNVDA",
	]
	if authorization:
		lines.append(f"Proxy-Authorization: {authorization}")
	return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def read_connect_response(sock, error_type=ProxyTunnelError):
	"""Return the status and headers of a CONNECT response.

	Reading stops on the header terminator so that the first bytes of the
	tunnelled stream are left in the socket for the TLS handshake.  Callers
	pass their own exception type to keep their error contract unchanged.
	"""
	data = bytearray()
	while not data.endswith(b"\r\n\r\n"):
		if len(data) >= MAX_HEADER_BYTES:
			raise error_type("The proxy response headers are too large")
		chunk = sock.recv(1)
		if not chunk:
			raise error_type("The proxy closed the CONNECT response early")
		data.extend(chunk)
	lines = bytes(data[:-4]).split(b"\r\n")
	try:
		status_line = lines.pop(0).decode("iso-8859-1")
		status = int(status_line.split(" ", 2)[1])
	except (IndexError, ValueError, UnicodeDecodeError) as error:
		raise error_type("The proxy returned an invalid HTTP response") from error
	headers = {}
	for line in lines:
		if b":" not in line:
			continue
		name, value = line.split(b":", 1)
		key = name.decode("iso-8859-1").strip().lower()
		text = value.decode("iso-8859-1").strip()
		headers[key] = f"{headers[key]}, {text}" if key in headers else text
	return status, headers


def drain_response_body(sock, headers):
	"""Consume the body of a rejected CONNECT response.

	Windows integrated authentication continues on the same socket, so the
	error page returned with a 407 has to be read before the next CONNECT.
	Otherwise it would be parsed as the status line of the next response.
	"""
	try:
		remaining = int(headers.get("content-length", 0))
	except (TypeError, ValueError):
		return
	while remaining > 0:
		chunk = sock.recv(min(_BODY_CHUNK_BYTES, remaining))
		if not chunk:
			return
		remaining -= len(chunk)


def open_socks_socket(settings, target_host, target_port, timeout):
	"""Return a socket tunnelled through the configured SOCKS proxy."""
	proxy_type_name = str(getattr(settings, "type", "")).lower()
	if proxy_type_name not in SOCKS_TYPES:
		raise ProxyTunnelError(f"Unsupported SOCKS proxy type: {proxy_type_name}")
	ProxyType, Proxy = _import_python_socks()
	proxy_type = {
		"socks4": ProxyType.SOCKS4,
		"socks4a": ProxyType.SOCKS4,
		"socks5": ProxyType.SOCKS5,
		"socks5h": ProxyType.SOCKS5,
	}[proxy_type_name]
	return Proxy.create(
		proxy_type=proxy_type,
		host=settings.host,
		port=int(settings.port),
		username=settings.username or None,
		password=settings.password or None,
		rdns=proxy_type_name in ("socks4a", "socks5h"),
	).connect(target_host, target_port, timeout=timeout)


def create_tls_context(insecure=False):
	"""Return the TLS client context used to wrap a relay socket."""
	context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
	context.minimum_version = ssl.TLSVersion.TLSv1_2
	if insecure:
		context.check_hostname = False
		context.verify_mode = ssl.CERT_NONE
	else:
		context.load_default_certs()
		context.check_hostname = True
		context.verify_mode = ssl.CERT_REQUIRED
	return context


def _import_python_socks():
	library_dir = str(Path(__file__).parent / _library_name())
	if library_dir not in sys.path:
		sys.path.insert(0, library_dir)
	try:
		from python_socks._types import ProxyType
		from python_socks.sync import Proxy
	finally:
		if sys.path and sys.path[0] == library_dir:
			sys.path.pop(0)
	return ProxyType, Proxy


def _library_name():
	try:
		import buildVersion

		return "lib64" if buildVersion.version_year >= 2026 else "lib32"
	except ImportError:
		return "lib64"
