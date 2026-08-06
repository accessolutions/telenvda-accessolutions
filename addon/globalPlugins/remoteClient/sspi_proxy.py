"""Optional Windows SSPI proxy authentication boundary.

The add-on remains usable without an SSPI package.  Deployments that provide
an SSPI implementation can subclass `SSPIProxyAuthenticator` and pass tokens
to their proxy adapter without putting credentials in the source tree.
"""


class SSPIProxyError(Exception):
	pass


class SSPIProxyAuthenticator:
	def __init__(self, mechanism):
		if mechanism not in ("negotiate", "ntlm"):
			raise ValueError(f"Unsupported SSPI mechanism: {mechanism}")
		self.mechanism = mechanism
		self.complete = False

	def initial_token(self):
		return None

	def next_token(self, challenge):
		raise SSPIProxyError(
			"Windows SSPI authentication requires an NVDA-compatible SSPI provider"
		)
