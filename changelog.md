This release brings the following changes:

* Handle server connections which break when client sends ALPN information. This should restore connectivity to nvdaremote.com and similar servers.
* Add native Windows SSPI authentication for NTLM and Kerberos/Negotiate HTTP proxy CONNECT handshakes used by WebSocket relays.

Important: some anti-virus software may fla parts of this add-on as malicious. Specifically, `url_handler.exe`, which opens `remote://` and `tele://` links. If you don't use this feature, you can safely quarantine or delete the file. Otherwise, you must add it as an exception.

SHA256:
