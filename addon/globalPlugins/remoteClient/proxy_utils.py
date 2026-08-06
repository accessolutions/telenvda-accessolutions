"""Proxy configuration helpers shared by relay and diagnostics."""

from dataclasses import dataclass, replace


PROXY_MODES = ("manual", "auto", "none")


SUPPORTED_PROXY_TYPES = ("http", "socks4", "socks4a", "socks5", "socks5h", "negotiate", "ntlm")


@dataclass(frozen=True)
class ProxySettings:
	host: str = ""
	port: int = 0
	type: str = "http"
	username: str = ""
	password: str = ""
	auto_detect: bool = False
	use_environment: bool = True

	@property
	def enabled(self):
		return bool(self.host and self.port)


def from_config(config):
	section = config.get("controlserver", config)
	mode = str(section.get("proxy_mode", "manual")).lower()
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


def resolve_for_url(settings, url):
	"""Resolve automatic Windows proxy settings for one destination URL."""
	if not settings.auto_detect:
		return settings
	try:
		from .windows_proxy import detect_proxy

		detected = detect_proxy(url)
	except Exception:
		return replace(settings, auto_detect=False)
	if detected is None:
		# Keep the existing environment-variable fallback if Windows detection
		# is unavailable or cannot resolve an automatic configuration.
		return replace(settings, auto_detect=False)
	return ProxySettings(
		host=detected.host,
		port=detected.port,
		type=detected.type,
		use_environment=False,
	)


def uses_sspi(settings):
	"""Return whether the proxy requires the Windows SSPI CONNECT path."""
	return settings.enabled and settings.type in ("negotiate", "ntlm")


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
