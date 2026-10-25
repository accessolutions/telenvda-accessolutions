"""Proxy configuration helpers shared by relay and diagnostics."""

import logging
import re
import socket
import threading
import time
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

log = logging.getLogger("proxy")

PROXY_MODES = ("manual", "auto", "none")


SUPPORTED_PROXY_TYPES = ("http", "socks4", "socks4a", "socks5", "socks5h", "negotiate", "ntlm")

#: How long a successful automatic resolution is reused before Windows is asked
#: again. WinHTTP may block for up to five seconds per question, which the
#: connector would otherwise pay at every attempt, every few seconds.
DETECTION_CACHE_SECONDS = 180.0
#: A failed resolution is reused for a shorter while: the network may be coming up.
DETECTION_FAILURE_CACHE_SECONDS = 30.0


@dataclass(frozen=True)
class ProxySettings:
	host: str = ""
	port: int = 0
	type: str = "http"
	username: str = ""
	password: str = ""
	auto_detect: bool = False
	use_environment: bool = True
	#: Other proxies worth trying when this one does not answer, in order.
	alternatives: tuple = ()
	#: True when this proxy came out of the automatic detection rather than
	#: from the settings of the add-on: what worked is then worth remembering.
	detected: bool = False

	@property
	def enabled(self):
		return bool(self.host and self.port)

	@property
	def key(self):
		"""The route without credentials, as stored in the configuration."""
		return f"{self.type}://{self.host}:{self.port}" if self.enabled else ""


def describe(settings):
	"""Return a short description of the proxy route for the logs: never the password."""
	if settings is None:
		return "none"
	if getattr(settings, "enabled", False):
		credentials = f"{settings.username}@" if getattr(settings, "username", "") else ""
		description = f"{settings.type}://{credentials}{settings.host}:{settings.port}"
		alternatives = getattr(settings, "alternatives", ())
		if alternatives:
			description += " (+%d alternative%s)" % (len(alternatives), "s" if len(alternatives) > 1 else "")
		return description
	if getattr(settings, "auto_detect", False):
		return "auto (unresolved)"
	return "direct"


def candidates(settings):
	"""Yield the proxy and then each of its alternatives, as standalone settings."""
	yield replace(settings, alternatives=())
	for alternative in settings.alternatives:
		yield replace(alternative, alternatives=())


def parse_key(key, detected=True):
	"""Return the settings described by ``type://host:port``, or None."""
	key = (key or "").strip()
	if not key:
		return None
	parsed = urlsplit(key if "://" in key else f"http://{key}")
	try:
		host, port = parsed.hostname, parsed.port
	except ValueError:
		return None
	if not host or not port:
		return None
	proxy_type = (parsed.scheme or "http").lower()
	if proxy_type not in SUPPORTED_PROXY_TYPES:
		proxy_type = "http"
	return ProxySettings(host=host, port=port, type=proxy_type, use_environment=False, detected=detected)


def from_config(config):
	section = config.get("controlserver", config)
	# The configuration schema defaults to automatic detection, so a missing key
	# must mean the same thing here rather than an empty manual proxy.
	mode = str(section.get("proxy_mode", "auto")).lower()
	if mode == "auto":
		return ProxySettings(auto_detect=True)
	if mode == "none":
		return ProxySettings(use_environment=False)
	return ProxySettings(
		host=section.get("proxy_host", ""),
		port=int(section.get("proxy_port", 0) or 0),
		type=str(section.get("proxy_type", "http")).lower(),
		username=section.get("proxy_username", ""),
		password=section.get("proxy_password", ""),
	)


def from_system(url):
	"""Read the proxy declared by Windows Internet settings or the environment.

	This is the fallback used when WinHTTP cannot resolve an automatic
	configuration. ``urllib`` reads the Windows registry as well as the
	``http_proxy`` / ``https_proxy`` variables, so a machine configured through
	Internet Options keeps working. Returns None when no proxy applies.
	"""
	try:
		from urllib.request import getproxies, proxy_bypass

		proxies = getproxies()
	except Exception:
		return None
	if not proxies:
		return None
	host = urlsplit(url).hostname or ""
	try:
		if host and proxy_bypass(host):
			return None
	except Exception:
		pass
	proxy_url = proxies.get("https") or proxies.get("http") or proxies.get("socks")
	if not proxy_url:
		return None
	parsed = urlsplit(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
	try:
		proxy_host = parsed.hostname
		proxy_port = parsed.port
	except ValueError:
		return None
	if not proxy_host:
		return None
	scheme = (parsed.scheme or "http").lower()
	if scheme.startswith("socks"):
		proxy_type = scheme if scheme in SUPPORTED_PROXY_TYPES else "socks5h"
		default_port = 1080
	else:
		proxy_type = "http"
		default_port = 8080
	return ProxySettings(
		host=proxy_host,
		port=proxy_port or default_port,
		type=proxy_type,
		username=parsed.username or "",
		password=parsed.password or "",
		use_environment=False,
	)


_decision_lock = threading.Lock()
_last_decisions = {}


def _log_decision(url, mode, settings, reason):
	"""Say which route was chosen for ``url`` and why.

	The connector retries every few seconds for as long as the user wants to be
	connected, and each attempt resolves the proxy again. The first decision for a
	URL, and every change of decision, is worth an info line in the NVDA log; the
	repeats only go to the daily log, as debug.
	"""
	description = describe(settings)
	with _decision_lock:
		previous = _last_decisions.get(url)
		_last_decisions[url] = (description, reason)
	level = logging.DEBUG if previous == (description, reason) else logging.INFO
	log.log(level, "Proxy for %s (mode %s): %s, %s", url, mode, description, reason)


def last_decision(url):
	"""Return ``(route, reason)`` of the last resolution for ``url``, or None.

	The diagnostics show it to the user: which proxy was chosen, and why, is the
	first thing to know when a connection fails on a managed network.
	"""
	with _decision_lock:
		return _last_decisions.get(url)


_cache_lock = threading.Lock()
#: url -> (expiry time, resolved settings)
_detection_cache = {}


def forget(url=None):
	"""Drop the cached automatic resolution of ``url``, or of every URL.

	Called when a connection through the resolved route failed: the next attempt
	asks Windows again rather than trusting an answer the network just denied.
	"""
	with _cache_lock:
		if url is None:
			_detection_cache.clear()
		else:
			_detection_cache.pop(url, None)


def _cached(url):
	with _cache_lock:
		entry = _detection_cache.get(url)
		if entry is None:
			return None
		expires_at, resolved = entry
		if time.monotonic() >= expires_at:
			_detection_cache.pop(url, None)
			return None
		return resolved


def _store(url, resolved, failed):
	duration = DETECTION_FAILURE_CACHE_SECONDS if failed else DETECTION_CACHE_SECONDS
	with _cache_lock:
		_detection_cache[url] = (time.monotonic() + duration, resolved)


def _memory_host(url):
	return (urlsplit(url).hostname or "").lower()


def _configuration():
	try:
		from . import configuration
	except Exception:
		return None
	return configuration


def remembered_proxy(url):
	"""Return the proxy which last worked for the relay of ``url``, or None."""
	configuration = _configuration()
	host = _memory_host(url)
	if configuration is None or not host:
		return None
	try:
		return parse_key(configuration.get_config()["proxy_memory"].get(host, ""))
	except Exception:
		log.debug("The remembered proxy for %s could not be read", host, exc_info=True)
		return None


def remember_working_proxy(url, settings):
	"""Record that ``settings`` carried a connection to the relay of ``url``.

	Only a detected proxy is worth writing down: a manual one is already in the
	settings. The memory serves when the detection fails later, on the login
	screen for instance, where Windows has no proxy settings to offer.
	"""
	if not (getattr(settings, "detected", False) and settings.enabled):
		return False
	configuration = _configuration()
	host = _memory_host(url)
	if configuration is None or not host:
		return False
	try:
		config = configuration.get_config()
		if config["proxy_memory"].get(host) == settings.key:
			return False
		config["proxy_memory"][host] = settings.key
		if not getattr(configuration, "readonly", False):
			config.write()
	except Exception:
		log.debug("The working proxy for %s could not be remembered", host, exc_info=True)
		return False
	log.info("Remembering that %s reaches %s, for the next time the detection fails", settings.key, host)
	return True


def _from_detected(detected):
	return ProxySettings(
		host=detected.host,
		port=detected.port,
		type=detected.type,
		use_environment=False,
		detected=True,
		alternatives=tuple(
			ProxySettings(
				host=alternative.host,
				port=alternative.port,
				type=alternative.type,
				use_environment=False,
				detected=True,
			)
			for alternative in getattr(detected, "alternatives", ())
		),
	)


def _append_alternative(settings, extra):
	"""Return ``settings`` with ``extra`` as its last alternative, unless already present."""
	if extra is None or not extra.enabled:
		return settings
	keys = {candidate.key for candidate in candidates(settings)}
	if extra.key in keys:
		return settings
	return replace(settings, alternatives=settings.alternatives + (replace(extra, alternatives=()),))


def resolve_for_url(settings, url):
	"""Resolve automatic Windows proxy settings for one destination URL."""
	if not settings.auto_detect:
		if settings.enabled:
			_log_decision(url, "manual", settings, "configured in TeleNVDA")
			return settings
		if not settings.use_environment:
			_log_decision(url, "none", settings, "proxy disabled in TeleNVDA")
			return settings
		# No proxy is configured for the add-on, but the machine may still
		# declare one. Resolving it here keeps every transport on the same
		# route instead of leaving each library to read the environment.
		system = from_system(url)
		if system is not None:
			_log_decision(url, "manual", system, "no proxy in TeleNVDA, taken from the system settings")
			return system
		_log_decision(url, "manual", settings, "no proxy in TeleNVDA nor in the system settings")
		return settings

	cached = _cached(url)
	if cached is not None:
		log.debug("Proxy for %s (mode auto): %s, from the cache", url, describe(cached))
		return cached
	resolved, failed = _detect(settings, url)
	_store(url, resolved, failed)
	return resolved


def _detect(settings, url):
	"""Ask Windows, then the environment, then the memory. Returns (settings, failed)."""
	detected = None
	try:
		try:
			from .windows_proxy import detect_proxy
		except ImportError:
			from windows_proxy import detect_proxy

		detected = detect_proxy(url)
	except Exception:
		log.warning(
			"Windows proxy detection raised for %s, falling back to the system settings",
			url,
			exc_info=True,
		)
		detected = None
	remembered = remembered_proxy(url)
	if detected is not None and detected.enabled:
		resolved = _append_alternative(_from_detected(detected), remembered)
		_log_decision(
			url,
			"auto",
			resolved,
			"resolved by Windows (%s)" % (getattr(detected, "source", "") or "WPAD, PAC or static proxy"),
		)
		return resolved, False
	# Windows reported a direct connection, or the detection is unavailable.
	# Fall back to the system-wide proxy so that machines configured only
	# through Internet Options or environment variables still connect.
	fallback = from_system(url)
	if fallback is not None:
		fallback = _append_alternative(replace(fallback, detected=True), remembered)
		_log_decision(
			url,
			"auto",
			fallback,
			"Windows resolution gave %s, taken from the registry or the environment variables instead"
			% ("direct" if detected is not None else "nothing"),
		)
		return fallback, False
	if detected is None and remembered is not None:
		_log_decision(
			url,
			"auto",
			remembered,
			"Windows resolution failed; reusing the proxy which carried the last connection",
		)
		return remembered, True
	direct = replace(settings, auto_detect=False)
	if detected is None:
		_log_decision(
			url,
			"auto",
			direct,
			"Windows resolution failed and no proxy is declared anywhere; "
			"a network which only allows its proxy will refuse this connection",
		)
		return direct, True
	_log_decision(url, "auto", direct, "Windows says this destination needs no proxy")
	return direct, False


def is_proxy_unreachable(error):
	"""Return whether ``error`` blames the proxy rather than the relay behind it.

	Only then is the next candidate of the list worth a try: a certificate the
	relay presents, or a 407 asking for credentials, will be the same whichever
	proxy carries the request.
	"""
	status = getattr(error, "status_code", None)
	if status is None:
		status = getattr(error, "status", None)
	if isinstance(status, int):
		# 407 wants credentials; 2xx/3xx/4xx came from the relay or a gateway
		# which was reached. A 5xx from the proxy itself means it cannot help.
		return status >= 500 or status == 403
	name = type(error).__name__
	if name == "WebSocketProxyException":
		return not re.search(r"status:\s*407\b", str(error))
	if name in ("WebSocketBadStatusException", "SSPIProxyError"):
		return False
	import ssl

	if isinstance(error, ssl.SSLError):
		return False
	return isinstance(error, (OSError, socket.timeout, ConnectionError))


def open_through_candidates(settings, url, opener):
	"""Call ``opener(candidate)`` with each proxy in turn until one answers.

	Returns ``(result, candidate)``. The candidate which worked is remembered
	when it came from the detection. When every candidate fails, the cached
	resolution of ``url`` is dropped so that the next attempt asks Windows again,
	and the error of the first candidate is raised: it is the route Windows
	preferred, so its failure is the one to report.
	"""
	ordered = list(candidates(settings))
	first_error = None
	for index, candidate in enumerate(ordered):
		try:
			result = opener(candidate)
		except Exception as error:
			if first_error is None:
				first_error = error
			remaining = len(ordered) - index - 1
			unreachable = is_proxy_unreachable(error)
			if remaining and unreachable:
				log.warning(
					"Proxy %s did not carry the connection to %s (%s: %s), trying the next of %d",
					describe(candidate),
					url,
					type(error).__name__,
					error,
					remaining,
				)
				continue
			if candidate.enabled and unreachable:
				forget(url)
			if unreachable and first_error is not error:
				log.warning(
					"None of the %d proxies reached %s; reporting the failure of the preferred one",
					len(ordered),
					url,
				)
				raise first_error
			raise
		if index:
			log.info("Proxy %s carried the connection to %s after %d failed", describe(candidate), url, index)
			_promote(url, settings, candidate)
		remember_working_proxy(url, candidate)
		return result, candidate
	raise RuntimeError("No proxy candidate to try")  # pragma: no cover - candidates() always yields one


def _promote(url, settings, candidate):
	"""Put the candidate which answered first in the cached resolution of ``url``."""
	if _cached(url) is None:
		return
	others = tuple(other for other in candidates(settings) if other.key != candidate.key)
	_store(url, replace(candidate, alternatives=others), False)


def uses_sspi(settings):
	"""Return whether the proxy requires the Windows SSPI CONNECT path."""
	return settings.enabled and settings.type in ("negotiate", "ntlm")


def allows_windows_authentication(settings):
	"""Return whether a rejected CONNECT may be retried with Windows credentials.

	An HTTP proxy that answers 407 wants authentication we did not provide, so
	the SSPI tunnel (NTLM / Kerberos) is worth a try before giving up.
	"""
	return bool(settings.enabled and settings.type == "http" and not settings.username)


def is_proxy_authentication_error(error):
	"""Return whether ``error`` is a proxy answering 407 to the CONNECT.

	The HTTPS path raises an error carrying the status. The WebSocket library
	raises its proxy exception with the status in the message only, in the form
	``failed CONNECT via proxy status: 407``, so that one is read from the text,
	but only for that exception type: a 407 quoted in any other error, a page
	title for instance, must not be mistaken for a proxy challenge.
	"""
	status = getattr(error, "status_code", None)
	if status is None:
		status = getattr(error, "status", None)
	if isinstance(status, int):
		return status == 407
	if type(error).__name__ == "WebSocketProxyException":
		return re.search(r"status:\s*407\b", str(error)) is not None
	return False


def needs_windows_authentication(settings, error):
	"""Return whether a connection failure was a proxy 407 worth retrying with SSPI."""
	if not allows_windows_authentication(settings):
		return False
	return is_proxy_authentication_error(error)


def with_windows_authentication(settings):
	"""Return the same proxy, negotiated through Windows integrated auth."""
	return replace(settings, type="negotiate")


def websocket_options(settings):
	"""Return websocket-client options for proxy types it supports natively."""
	if not settings.enabled:
		return {} if settings.use_environment else {"http_no_proxy": ["*"]}
	if settings.type in ("negotiate", "ntlm"):
		return {}
	options = {
		"http_proxy_host": settings.host,
		"http_proxy_port": settings.port,
		"proxy_type": settings.type,
	}
	if settings.username:
		options["http_proxy_auth"] = (settings.username, settings.password)
	return options
