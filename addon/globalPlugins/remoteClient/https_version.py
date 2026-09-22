"""Choose between HTTPS relay versions and remember streaming failures."""

AUTO = "auto"
V1 = "v1"
V2 = "v2"

_failed_v2 = set()


def network_key(address, proxy_settings) -> str:
	"""Identify a relay together with the proxy route used to reach it."""
	host, port = address
	if proxy_settings is not None and proxy_settings.host and proxy_settings.port:
		route = "%s:%s" % (proxy_settings.host, proxy_settings.port)
	else:
		route = "direct"
	return "%s:%s|%s" % (host, port, route)


def versions_to_try(preference: str, key: str) -> list[str]:
	if preference == V1:
		return [V1]
	if preference == V2:
		return [V2]
	if key in _failed_v2:
		return [V1]
	return [V2, V1]


def remember_failure(key: str) -> None:
	_failed_v2.add(key)


def remember_success(key: str) -> None:
	_failed_v2.discard(key)


def forget_all() -> None:
	"""Clear the process-local probe results after a route change."""
	_failed_v2.clear()
