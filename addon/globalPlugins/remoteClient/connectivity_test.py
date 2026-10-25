"""Connectivity checks for TCP/TLS, WebSocket and HTTPS relay endpoints."""

import http.client
import socket
import ssl
import time

from . import connectivity_log
from . import configuration
from . import https_connection
from . import https_protocol
from . import proxy_utils
from . import ws_protocol
from .transport import WebSocketRelayTransport

try:
	import buildVersion
except ImportError:
	buildVersion = None


def test_endpoint(host, port, transport_type="tcp", ws_path="/", timeout=10):
	started = time.monotonic()
	port = int(port)
	result = {
		"host": host,
		"port": port,
		"transport": transport_type,
		"success": False,
	}
	if transport_type == "https":
		return _test_https_endpoint(host, port, ws_path, timeout, result, started)
	try:
		socket.getaddrinfo(host, port)
		result["dns"] = True
		if transport_type == "websocket":
			transport = WebSocketRelayTransport(
				serializer=_NullSerializer(),
				address=(host, int(port)),
				ws_path=ws_path,
				insecure=False,
				timeout=timeout,
			)
			_record_proxy(
				result,
				ws_protocol.websocket_url(host, int(port), ws_protocol.normalize_path(ws_path)),
			)
			socket_obj = transport.create_websocket()
			socket_obj.close()
		else:
			# The TCP transport dials the relay directly: a proxy, configured or
			# detected, never applies to it. Saying so in the result spares a
			# long search when only this transport fails behind a proxy.
			result["proxy"] = "not applicable (TCP connects directly)"
			context = ssl.create_default_context()
			with socket.create_connection((host, int(port)), timeout=timeout) as raw:
				with context.wrap_socket(raw, server_hostname=host):
					pass
		result["success"] = True
		result["message"] = "Connection and TLS validation succeeded"
	except Exception as error:
		result["message"] = str(error)
	return _complete_result(result, started)


def _test_https_endpoint(host, port, path, timeout, result, started):
	try:
		socket.getaddrinfo(host, port)
		result["dns"] = True
	except OSError:
		return _fail_result(result, started, "dns_unavailable", "DNS resolution failed")

	connection = None
	proxy_settings = None
	try:
		relay_path = https_protocol.normalize_https_path(path)
		url = https_protocol.https_url(host, port, relay_path)
		proxy_settings = _record_proxy(result, url)
		connection = https_connection.open_https_connection(
			(host, port),
			proxy_settings,
			timeout=timeout,
			insecure=False,
		)
		create_status, create_headers, create_body = _https_request(
			connection,
			"POST",
			https_protocol.sessions_path(relay_path),
			https_protocol.encode_session_request(),
		)
		if create_status != 201:
			return _fail_http_result(
				result,
				started,
				create_status,
				proxy_settings,
				create_headers,
				create_body,
			)
		if _is_html_response(create_headers, create_body):
			return _fail_result(
				result,
				started,
				"html_response",
				"The HTTPS endpoint returned HTML instead of JSON",
			)
		try:
			session = https_protocol.decode_session_response(create_body)
		except https_protocol.HTTPSProtocolError as error:
			code = "protocol_mismatch" if error.reason == "unsupported_protocol" else "invalid_json"
			return _fail_result(
				result,
				started,
				code,
				"The HTTPS endpoint returned an invalid session response",
			)
		delete_status, delete_headers, delete_body = _https_request(
			connection,
			"DELETE",
			https_protocol.session_path(relay_path, session.session_id),
			token=session.token,
		)
		if delete_status != 204:
			return _fail_http_result(
				result,
				started,
				delete_status,
				proxy_settings,
				delete_headers,
				delete_body,
				failure_code="session_cleanup_failed",
			)
		result["success"] = True
		result["message"] = "HTTPS relay session creation and deletion succeeded"
	except Exception as error:
		return _fail_exception_result(result, started, error, proxy_settings)
	finally:
		if connection is not None:
			try:
				connection.close()
			except Exception:
				pass
	return _complete_result(result, started)


def _record_proxy(result, url):
	"""Resolve the proxy for ``url`` and write the route and its reason into the result."""
	proxy_settings = proxy_utils.resolve_for_url(
		proxy_utils.from_config(configuration.get_config()),
		url,
	)
	result["proxy"] = proxy_utils.describe(proxy_settings)
	decision = proxy_utils.last_decision(url)
	if decision is not None:
		result["proxy_reason"] = decision[1]
	return proxy_settings


def _https_request(connection, method, path, body=None, token=None):
	version = getattr(buildVersion, "version_year", "unknown")
	headers = {
		"Accept": "application/json",
		"User-Agent": "TeleNVDA/%s" % version,
		"X-TeleNVDA-Transport": "https-long-poll/1",
	}
	if body is not None:
		headers["Content-Type"] = "application/json"
	if token is not None:
		headers["Authorization"] = "Bearer %s" % token
	connection.request(method, path, body=body, headers=headers)
	response = connection.getresponse()
	return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()


def _is_html_response(headers, body):
	content_type = headers.get("content-type", "").lower()
	return "text/html" in content_type or body.lstrip().lower().startswith(
		(b"<!doctype html", b"<html", b"<head"),
	)


def _fail_http_result(result, started, status, proxy_settings, headers, body, failure_code=None):
	if status == 403:
		code, message = "http_forbidden", "HTTP filtering returned status 403"
	elif status == 404:
		code, message = "api_unavailable", "The HTTPS relay API is disabled or the route was not found"
	elif status == 407:
		code, message = "proxy_authentication_required", "The HTTP proxy requires authentication"
	elif status == 429:
		code, message = "server_limit", "The HTTPS relay server limit was reached"
	elif _is_html_response(headers, body):
		code, message = "html_response", "The HTTPS endpoint returned HTML instead of JSON"
	else:
		code = failure_code or "http_error"
		message = "The HTTPS endpoint returned status %s" % status
	return _fail_result(result, started, code, message, status=status)


def _fail_exception_result(result, started, error, proxy_settings):
	if https_connection.is_certificate_verification_error(error):
		return _fail_result(result, started, "certificate_untrusted", "The TLS certificate is not trusted")
	if isinstance(error, https_connection.HTTPSProxyAuthenticationError):
		return _fail_result(
			result,
			started,
			"proxy_authentication_required",
			"The HTTP proxy requires authentication",
			status=407,
		)
	if isinstance(error, https_connection.HTTPSProxyError):
		return _fail_result(
			result,
			started,
			"proxy_unreachable",
			"The HTTP proxy rejected the connection",
			status=error.status,
		)
	if isinstance(error, https_protocol.HTTPSProtocolError):
		return _fail_result(result, started, error.reason, "The HTTPS relay request could not be validated")
	if isinstance(error, ssl.SSLError):
		return _fail_result(result, started, "tls_failed", "TLS negotiation failed")
	if getattr(proxy_settings, "enabled", False) and isinstance(error, (OSError, http.client.HTTPException)):
		return _fail_result(result, started, "proxy_unreachable", "The HTTP proxy could not be reached")
	return _fail_result(result, started, "connection_failed", "The HTTPS endpoint could not be reached")


def _fail_result(result, started, error_code, message, status=None):
	result["error_code"] = error_code
	if status is not None:
		result["status"] = status
	result["message"] = message
	return _complete_result(result, started)


def _complete_result(result, started):
	result["duration_ms"] = round((time.monotonic() - started) * 1000)
	connectivity_log.write_result(result)
	return result


def run_async(host, port, transport_type="tcp", ws_path="/", callback=None):
	import threading

	def worker():
		result = test_endpoint(host, port, transport_type, ws_path)
		if callback:
			callback(result)

	thread = threading.Thread(target=worker, name="TeleNVDA connectivity test", daemon=True)
	thread.start()
	return thread


class _NullSerializer:
	def serialize(self, **kwargs):
		return b""

	def deserialize(self, value):
		return {}
