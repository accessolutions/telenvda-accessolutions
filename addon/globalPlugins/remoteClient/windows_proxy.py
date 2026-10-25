"""Windows proxy auto-detection for a destination URL."""

from __future__ import annotations

import ctypes
import fnmatch
import logging
import os
from ctypes import wintypes
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("proxy")

_WINHTTP_ACCESS_TYPE_NO_PROXY = 1
_WINHTTP_AUTO_PROXY_FLAG_AUTO_DETECT = 0x00000001
_WINHTTP_AUTO_PROXY_FLAG_CONFIG_URL = 0x00000002
_WINHTTP_AUTO_DETECT_TYPE_DHCP = 0x00000001
_WINHTTP_AUTO_DETECT_TYPE_DNS_A = 0x00000002

#: WinHTTP error codes met during proxy detection, named so that the log says
#: what went wrong rather than a bare number. See winhttp.h.
_WINHTTP_ERRORS = {
	12002: "ERROR_WINHTTP_TIMEOUT",
	12005: "ERROR_WINHTTP_INVALID_URL",
	12006: "ERROR_WINHTTP_UNRECOGNIZED_SCHEME",
	12007: "ERROR_WINHTTP_NAME_NOT_RESOLVED",
	12015: "ERROR_WINHTTP_LOGIN_FAILURE",
	12017: "ERROR_WINHTTP_OPERATION_CANCELLED",
	12029: "ERROR_WINHTTP_CANNOT_CONNECT",
	12030: "ERROR_WINHTTP_CONNECTION_ERROR",
	12166: "ERROR_WINHTTP_BAD_AUTO_PROXY_SCRIPT",
	12167: "ERROR_WINHTTP_UNABLE_TO_DOWNLOAD_SCRIPT",
	12178: "ERROR_WINHTTP_AUTO_PROXY_SERVICE_ERROR",
	12180: "ERROR_WINHTTP_AUTODETECTION_FAILED",
}

#: Hints matching the error codes above, for the person reading the log.
_WINHTTP_HINTS = {
	12166: "the PAC script has a syntax or runtime error",
	12167: (
		"the PAC script URL cannot be downloaded "
		"(unreachable, or a file:// or UNC path, which WinHTTP does not support)"
	),
	12178: "the WinHTTP Web Proxy Auto-Discovery service (WinHttpAutoProxySvc) is stopped or disabled",
	12180: "WPAD found no proxy through DHCP option 252 nor DNS 'wpad' host",
	12002: "the PAC download or WPAD lookup exceeded 5 seconds",
}


def _last_error():
	"""Return the last Win32 error as ``code (NAME: hint)``."""
	code = ctypes.get_last_error()
	name = _WINHTTP_ERRORS.get(code)
	if name is None:
		try:
			name = ctypes.FormatError(code).strip()
		except Exception:
			name = "unknown"
	hint = _WINHTTP_HINTS.get(code)
	return f"{code} ({name}{': ' + hint if hint else ''})"


@dataclass(frozen=True)
class DetectedProxy:
	"""Proxy selected by Windows for a destination URL.

	``alternatives`` holds the following entries of the proxy list Windows
	returned, in order: a PAC script commonly answers ``PROXY a:8080; PROXY
	b:8080; DIRECT`` and a browser moves on to the next one when the first does
	not answer. ``source`` says where the decision came from, for the logs.
	"""

	host: str = ""
	port: int = 0
	type: str = "negotiate"
	alternatives: tuple = ()
	source: str = ""

	@property
	def enabled(self):
		return bool(self.host and self.port)


@dataclass(frozen=True)
class _InternetSettings:
	"""Proxy settings of Windows, read from the user profile or from the machine."""

	auto_detect: bool = False
	auto_config_url: str = ""
	proxy: str = ""
	bypass: str = ""
	source: str = "user"
	#: True when a policy makes these settings prevail over the user's.
	authoritative: bool = False

	@property
	def automatic(self):
		return bool(self.auto_detect or self.auto_config_url)

	@property
	def empty(self):
		return not (self.automatic or self.proxy)


class _CurrentUserIEProxyConfig(ctypes.Structure):
	_fields_ = [
		("fAutoDetect", wintypes.BOOL),
		("lpszAutoConfigUrl", ctypes.c_void_p),
		("lpszProxy", ctypes.c_void_p),
		("lpszProxyBypass", ctypes.c_void_p),
	]


class _AutoProxyOptions(ctypes.Structure):
	_fields_ = [
		("dwFlags", wintypes.DWORD),
		("dwAutoDetectFlags", wintypes.DWORD),
		("lpszAutoConfigUrl", ctypes.c_wchar_p),
		("lpvReserved", ctypes.c_void_p),
		("dwReserved", wintypes.DWORD),
		("fAutoLogonIfChallenged", wintypes.BOOL),
	]


class _ProxyInfo(ctypes.Structure):
	_fields_ = [
		("dwAccessType", wintypes.DWORD),
		("lpszProxy", ctypes.c_void_p),
		("lpszProxyBypass", ctypes.c_void_p),
	]


def _read_string(pointer):
	return ctypes.wstring_at(pointer) if pointer else ""


def _free_string(kernel32, pointer):
	if pointer:
		kernel32.GlobalFree(pointer)


def _load_api():
	if os.name != "nt":
		return None
	try:
		winhttp = ctypes.WinDLL("winhttp", use_last_error=True)
		kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
	except OSError:
		return None

	winhttp.WinHttpGetIEProxyConfigForCurrentUser.argtypes = [
		ctypes.POINTER(_CurrentUserIEProxyConfig),
	]
	winhttp.WinHttpGetIEProxyConfigForCurrentUser.restype = wintypes.BOOL
	winhttp.WinHttpOpen.argtypes = [
		ctypes.c_wchar_p,
		wintypes.DWORD,
		ctypes.c_wchar_p,
		ctypes.c_wchar_p,
		wintypes.DWORD,
	]
	winhttp.WinHttpOpen.restype = ctypes.c_void_p
	winhttp.WinHttpSetTimeouts.argtypes = [
		ctypes.c_void_p,
		ctypes.c_int,
		ctypes.c_int,
		ctypes.c_int,
		ctypes.c_int,
	]
	winhttp.WinHttpSetTimeouts.restype = wintypes.BOOL
	winhttp.WinHttpGetProxyForUrl.argtypes = [
		ctypes.c_void_p,
		ctypes.c_wchar_p,
		ctypes.POINTER(_AutoProxyOptions),
		ctypes.POINTER(_ProxyInfo),
	]
	winhttp.WinHttpGetProxyForUrl.restype = wintypes.BOOL
	winhttp.WinHttpCloseHandle.argtypes = [ctypes.c_void_p]
	winhttp.WinHttpCloseHandle.restype = wintypes.BOOL
	kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
	kernel32.GlobalFree.restype = ctypes.c_void_p
	return winhttp, kernel32


def _is_bypassed(host, bypass_list):
	if not bypass_list:
		return False
	host = host.lower().rstrip(".")
	for raw_pattern in bypass_list.replace(";", " ").split():
		pattern = raw_pattern.strip().lower()
		if not pattern:
			continue
		if pattern == "<local>" and "." not in host:
			return True
		if fnmatch.fnmatchcase(host, pattern):
			return True
	return False


def _to_winhttp_url(url):
	"""Map the WebSocket schemes to HTTP ones.

	WinHttpGetProxyForUrl only understands http and https; it fails with
	ERROR_WINHTTP_UNRECOGNIZED_SCHEME for a ``wss`` URL, which would silently
	disable proxy detection for WebSocket connections.
	"""
	parts = urlsplit(url)
	scheme = parts.scheme.lower()
	if scheme == "wss":
		scheme = "https"
	elif scheme == "ws":
		scheme = "http"
	return urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _parse_proxy_endpoint(value, scheme, source=""):
	value = value.strip()
	if not value:
		return None
	is_socks = scheme.lower() == "socks"
	parsed = urlsplit(value if "://" in value else f"//{value}")
	try:
		host = parsed.hostname
		port = parsed.port or (1080 if is_socks else 8080)
	except ValueError:
		return None
	if not host:
		return None
	# A plain HTTP CONNECT tunnel is attempted first; the transport falls back to
	# Windows integrated authentication only when the proxy answers 407.
	proxy_type = "socks5" if is_socks else "http"
	return DetectedProxy(host=host, port=port, type=proxy_type, source=source)


def _preferred_scheme(url):
	scheme = urlsplit(url).scheme.lower()
	if scheme == "wss":
		return "https"
	if scheme == "ws":
		return "http"
	return scheme or "https"


def _parse_proxy_candidates(proxy_text, url, source=""):
	"""Return every usable proxy of a Windows proxy list, in order of preference.

	Two formats exist. The Internet settings use ``scheme=host:port`` entries
	separated by semicolons, one per protocol, possibly with a bare default. A
	PAC script returns ``host:port;host:port`` (WinHTTP strips the ``PROXY``
	keywords): several proxies to try one after the other. The entries dedicated
	to the scheme of the URL come first, then the generic ones.
	"""
	preferred = _preferred_scheme(url)
	ranked = {}
	order = (preferred, "https", "http", "default", "socks")
	for item in proxy_text.split(";"):
		item = item.strip()
		if not item:
			continue
		if "=" in item:
			scheme, value = item.split("=", 1)
			scheme = scheme.strip().lower()
		else:
			scheme, value = "default", item
		proxy = _parse_proxy_endpoint(value, scheme, source)
		if proxy is None:
			continue
		rank = order.index(scheme) if scheme in order else len(order)
		ranked.setdefault(rank, []).append(proxy)
	candidates = []
	seen = set()
	for rank in sorted(ranked):
		for proxy in ranked[rank]:
			key = (proxy.host.lower(), proxy.port, proxy.type)
			if key in seen:
				continue
			seen.add(key)
			candidates.append(proxy)
	return candidates


def _with_alternatives(candidates):
	"""Turn an ordered candidate list into one proxy carrying the others."""
	if not candidates:
		return None
	first, rest = candidates[0], tuple(candidates[1:])
	return DetectedProxy(
		host=first.host,
		port=first.port,
		type=first.type,
		alternatives=rest,
		source=first.source,
	)


def _parse_proxy_list(proxy_text, url, source=""):
	return _with_alternatives(_parse_proxy_candidates(proxy_text, url, source))


def _get_proxy_for_url(winhttp, kernel32, session, url, flags, auto_detect_flags, auto_config_url, method):
	"""Run WinHttpGetProxyForUrl once and translate its answer.

	Returns a DetectedProxy (possibly direct), or None when the call failed.
	"""
	options = _AutoProxyOptions(
		dwFlags=flags,
		dwAutoDetectFlags=auto_detect_flags,
		lpszAutoConfigUrl=auto_config_url or None,
		fAutoLogonIfChallenged=True,
	)
	proxy_info = _ProxyInfo()
	if not winhttp.WinHttpGetProxyForUrl(session, url, ctypes.byref(options), ctypes.byref(proxy_info)):
		log.warning(
			"Windows could not resolve a proxy for %s through %s: error %s",
			url,
			method,
			_last_error(),
		)
		return None
	try:
		source = "wpad" if flags & _WINHTTP_AUTO_PROXY_FLAG_AUTO_DETECT else "pac"
		if proxy_info.dwAccessType == _WINHTTP_ACCESS_TYPE_NO_PROXY:
			log.debug("Windows resolved %s through %s: direct connection", url, method)
			return DetectedProxy(source=source)
		proxy_text = _read_string(proxy_info.lpszProxy)
		proxy = _parse_proxy_list(proxy_text, url, source)
		if proxy is None:
			log.warning(
				"Windows resolved %s through %s to %r, which contains no usable proxy; going direct",
				url,
				method,
				proxy_text,
			)
			return DetectedProxy(source=source)
		log.debug(
			"Windows resolved %s through %s to %r: using %s:%s, then %d alternative(s)",
			url,
			method,
			proxy_text,
			proxy.host,
			proxy.port,
			len(proxy.alternatives),
		)
		return proxy
	finally:
		_free_string(kernel32, proxy_info.lpszProxy)
		_free_string(kernel32, proxy_info.lpszProxyBypass)


def _query_automatic_proxy(winhttp, kernel32, session, url, settings):
	"""Resolve the proxy through WPAD and the PAC script, as Windows would.

	When both are configured and the combined request fails, the PAC script is
	asked on its own: a WPAD lookup which times out, or a stopped auto-discovery
	service, must not take the perfectly good script down with it. This is the
	retry Microsoft recommends for WinHttpGetProxyForUrl.
	"""
	flags = 0
	auto_detect_flags = 0
	if settings.auto_detect:
		flags |= _WINHTTP_AUTO_PROXY_FLAG_AUTO_DETECT
		auto_detect_flags = _WINHTTP_AUTO_DETECT_TYPE_DHCP | _WINHTTP_AUTO_DETECT_TYPE_DNS_A
	if settings.auto_config_url:
		flags |= _WINHTTP_AUTO_PROXY_FLAG_CONFIG_URL
	if not flags:
		return None

	method = " + ".join(
		name
		for name, present in (
			("WPAD", settings.auto_detect),
			(f"PAC {settings.auto_config_url}", settings.auto_config_url),
		)
		if present
	)
	result = _get_proxy_for_url(
		winhttp,
		kernel32,
		session,
		url,
		flags,
		auto_detect_flags,
		settings.auto_config_url,
		method,
	)
	if result is not None or not (settings.auto_detect and settings.auto_config_url):
		return result
	log.info("Retrying the proxy resolution of %s with the PAC script alone, without WPAD", url)
	return _get_proxy_for_url(
		winhttp,
		kernel32,
		session,
		url,
		_WINHTTP_AUTO_PROXY_FLAG_CONFIG_URL,
		0,
		settings.auto_config_url,
		f"PAC {settings.auto_config_url} alone",
	)


def _read_user_settings(winhttp, kernel32):
	"""Read the Internet settings of the current user, or None when Windows refuses."""
	config = _CurrentUserIEProxyConfig()
	if not winhttp.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(config)):
		log.warning(
			"Windows refused to give the Internet proxy settings of this user: error %s",
			_last_error(),
		)
		return None
	try:
		return _InternetSettings(
			auto_detect=bool(config.fAutoDetect),
			auto_config_url=_read_string(config.lpszAutoConfigUrl),
			proxy=_read_string(config.lpszProxy),
			bypass=_read_string(config.lpszProxyBypass),
			source="user",
		)
	finally:
		_free_string(kernel32, config.lpszAutoConfigUrl)
		_free_string(kernel32, config.lpszProxy)
		_free_string(kernel32, config.lpszProxyBypass)


_INTERNET_SETTINGS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings"
_INTERNET_SETTINGS_POLICY_KEY = r"SOFTWARE\Policies\Microsoft\Windows\CurrentVersion\Internet Settings"
#: Flag of the ``DefaultConnectionSettings`` blob meaning "detect settings automatically".
_CONNECTION_FLAG_AUTO_DETECT = 0x08


def _registry_value(root, key, name, default=None):
	import winreg

	try:
		with winreg.OpenKey(root, key) as handle:
			value, _kind = winreg.QueryValueEx(handle, name)
			return value
	except OSError:
		return default


def _read_machine_settings():
	"""Read the Internet settings stored for the whole machine, or None.

	The group policy "Make proxy settings per-machine (rather than per-user)"
	moves them from the user profile to HKEY_LOCAL_MACHINE, where
	WinHttpGetIEProxyConfigForCurrentUser does not look. The same hive is also
	all that is visible from the login screen, where NVDA runs as SYSTEM. The
	policy hive takes precedence over the plain one when it holds a value.
	"""
	if os.name != "nt":
		return None
	import winreg

	per_user = _registry_value(
		winreg.HKEY_LOCAL_MACHINE,
		_INTERNET_SETTINGS_POLICY_KEY,
		"ProxySettingsPerUser",
	)
	authoritative = per_user == 0
	source = "machine (per-machine policy)" if authoritative else "machine"

	def value(name, default=None):
		policy = _registry_value(winreg.HKEY_LOCAL_MACHINE, _INTERNET_SETTINGS_POLICY_KEY, name)
		if policy is not None:
			return policy
		return _registry_value(winreg.HKEY_LOCAL_MACHINE, _INTERNET_SETTINGS_KEY, name, default)

	auto_detect = False
	blob = _registry_value(
		winreg.HKEY_LOCAL_MACHINE,
		_INTERNET_SETTINGS_KEY + r"\Connections",
		"DefaultConnectionSettings",
	)
	if isinstance(blob, (bytes, bytearray)) and len(blob) >= 12:
		auto_detect = bool(int.from_bytes(blob[8:12], "little") & _CONNECTION_FLAG_AUTO_DETECT)
	enabled = value("ProxyEnable", 0)
	try:
		enabled = int(enabled or 0)
	except (TypeError, ValueError):
		enabled = 0
	settings = _InternetSettings(
		auto_detect=auto_detect,
		auto_config_url=str(value("AutoConfigURL", "") or ""),
		proxy=str(value("ProxyServer", "") or "") if enabled else "",
		bypass=str(value("ProxyOverride", "") or ""),
		source=source,
		authoritative=authoritative,
	)
	if settings.empty:
		return None
	return settings


def _resolve_with_settings(winhttp, kernel32, session, url, settings):
	"""Apply one set of Internet settings to the URL. None means "nothing usable"."""
	log.debug(
		"Windows Internet settings (%s) for %s: auto-detect (WPAD)=%s, PAC=%r, static proxy=%r, bypass=%r",
		settings.source,
		url,
		"on" if settings.auto_detect else "off",
		settings.auto_config_url,
		settings.proxy,
		settings.bypass,
	)
	if settings.automatic:
		automatic = _query_automatic_proxy(winhttp, kernel32, session, url, settings)
		if automatic is not None:
			return automatic

	host = urlsplit(url).hostname or ""
	if settings.proxy and not _is_bypassed(host, settings.bypass):
		proxy = _parse_proxy_list(settings.proxy, url, "static " + settings.source)
		if proxy is None:
			log.warning("The static Windows proxy %r contains no usable entry for %s", settings.proxy, url)
		else:
			log.debug(
				"Using the static Windows proxy %s:%s for %s (%d alternative(s))",
				proxy.host,
				proxy.port,
				url,
				len(proxy.alternatives),
			)
		return proxy
	if settings.proxy:
		log.debug("The static Windows proxy is bypassed for %s (bypass list %r)", host, settings.bypass)
	if not settings.automatic:
		return DetectedProxy(source="direct " + settings.source)
	log.warning(
		"Automatic proxy resolution failed for %s and the %s settings declare no static proxy%s",
		url,
		settings.source,
		" applying to this host" if settings.proxy else "",
	)
	return None


def detect_proxy(url):
	"""Return the Windows-selected proxy, direct mode, or None if unavailable.

	The settings of the current user are tried first. When they are empty or
	cannot be applied, the machine-wide ones are consulted: this is where a group
	policy stores them, and the only place the login screen can read.
	"""
	api = _load_api()
	if api is None:
		log.debug("Windows proxy detection is unavailable: winhttp.dll could not be loaded")
		return None
	url = _to_winhttp_url(url)
	winhttp, kernel32 = api
	winhttp._kernel32 = kernel32

	session = winhttp.WinHttpOpen("TeleNVDA proxy detection", _WINHTTP_ACCESS_TYPE_NO_PROXY, None, None, 0)
	if not session:
		log.warning("WinHttpOpen failed, proxy detection skipped: error %s", _last_error())
		return None
	try:
		winhttp.WinHttpSetTimeouts(session, 5000, 5000, 5000, 5000)
		user = _read_user_settings(winhttp, kernel32)
		machine = None
		try:
			machine = _read_machine_settings()
		except Exception:
			log.debug("The machine-wide Internet settings could not be read", exc_info=True)

		# The machine hive is the reference when a policy says so. Otherwise it
		# is only a fallback, for a user profile which has nothing to say or
		# whose automatic resolution failed.
		if machine is not None and machine.authoritative:
			log.info(
				"A policy makes the proxy settings per-machine: using the machine-wide settings for %s",
				url,
			)
			result = _resolve_with_settings(winhttp, kernel32, session, url, machine)
			if result is not None:
				return result
			machine = None

		result = None
		if user is not None and not user.empty:
			result = _resolve_with_settings(winhttp, kernel32, session, url, user)
			if result is not None:
				return result
		elif user is not None:
			log.debug(
				"Windows has no proxy configured for this user (no WPAD, no PAC, no static proxy); "
				"this is what the login screen and services see when the settings belong to another user",
			)

		if machine is not None:
			log.info("Consulting the machine-wide Internet settings (%s) for %s", machine.source, url)
			result = _resolve_with_settings(winhttp, kernel32, session, url, machine)
			if result is not None:
				return result
		if user is not None and user.empty and machine is None:
			return DetectedProxy(source="direct user")
		log.warning(
			"No Windows proxy setting could be applied to %s: "
			"TeleNVDA will try the environment variables, then a remembered proxy, then a direct connection",
			url,
		)
		return None
	finally:
		winhttp.WinHttpCloseHandle(session)
