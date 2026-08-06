"""Proxy configuration helpers shared by relay and diagnostics."""

from dataclasses import dataclass


SUPPORTED_PROXY_TYPES = ("http", "socks4", "socks4a", "socks5", "socks5h", "negotiate", "ntlm")


@dataclass(frozen=True)
class ProxySettings:
	host: str = ""
	port: int = 0
	type: str = "http"
	username: str = ""
	password: str = ""

	@property
	def enabled(self):
		return bool(self.host and self.port)


def from_config(config):
	section = config.get("controlserver", config)
	return ProxySettings(
		host=section.get("proxy_host", ""),
		port=int(section.get("proxy_port", 0) or 0),
		type=str(section.get("proxy_type", "http")).lower(),
		username=section.get("proxy_username", ""),
		password=section.get("proxy_password", ""),
	)


def uses_sspi(settings):
	"""Return whether the proxy requires the Windows SSPI CONNECT path."""
	return settings.enabled and settings.type in ("negotiate", "ntlm")


def websocket_options(settings):
	"""Return websocket-client options for proxy types it supports natively."""
	if not settings.enabled:
		return {}
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
