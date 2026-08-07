This release brings the following changes:
* Handle server connections which break when client sends ALPN information. This should restore connectivity to nvdaremote.com and similar servers.
* Add native Windows SSPI authentication for NTLM and Kerberos/Negotiate HTTP proxy CONNECT handshakes used by WebSocket relays.
* Check GitHub Releases for TeleNVDA updates at startup or on demand, with stable and development channels, SHA-256 verification, and confirmation before installation.
* Fix automatic updates that failed to complete with an "access denied" error by marking the previous version for removal before installing the downloaded package, and recover installations that were already stuck pending.
* Automatically trust and remember previously unknown server certificate fingerprints so manual and automatic connections are not blocked by a confirmation dialog.
* Add a configurable inactivity timeout for automatic connections and a configurable folder for received screenshots.

Important: some anti-virus software may flag parts of this add-on as malicious. Specifically, `url_handler.exe`, which opens `remote://` and `tele://` links. If you don't use this feature, you can safely quarantine or delete the file. Otherwise, you must add it as an exception.

SHA256:
