This release brings the following changes:
* Handle server connections which break when client sends ALPN information. This should restore connectivity to nvdaremote.com and similar servers.
* Add native Windows SSPI authentication for NTLM and Kerberos/Negotiate HTTP proxy CONNECT handshakes used by WebSocket relays.
* Check GitHub Releases for TeleNVDA stable updates at startup or on demand, with SHA-256 verification and confirmation before installation.
* Fix automatic updates that failed to complete with an "access denied" error by marking the previous version for removal before installing the downloaded package, and recover installations that were already stuck pending.
* Automatically trust and remember previously unknown server certificate fingerprints so manual and automatic connections are not blocked by a confirmation dialog.
* Add a configurable inactivity timeout for automatic connections and a configurable folder for received screenshots.
* Add keyboard gestures for remote screenshots: NVDA+Control+Shift+P for the native capture and Windows+Alt+P for the PowerShell capture. Both work from the controlling and the controlled computer.
* Fix the PowerShell screenshot request, which silently used the native capture method instead.
* Fix the PowerShell screenshot request, which did nothing when the controlled computer ran a standard TeleNVDA: the capture method is now sent as a parameter of the usual screenshot request, so those computers answer with their native capture instead of ignoring the request.
* Make the PowerShell screenshot work with a controlled computer running the NVDA Remote add-on shipped with NVDA or the original TeleNVDA: when nothing answers the screenshot request, the capture is driven through the clipboard and keyboard messages of the standard protocol. Known issue: this compatible capture does not work yet, the Run dialog is never opened on the controlled computer. It will be fixed in a later release.
* Fix the screenshot gestures, which were forwarded to the controlled computer instead of being handled locally while remote control was active.
* Fall back to the native capture when PowerShell is unavailable or blocked on the controlled computer, and run PowerShell from its absolute path so a restricted PATH no longer prevents the capture.
* Encode remote screenshots as JPEG instead of the raw bitmap before Base64 encoding, so that captures transfer faster.

Important: some anti-virus software may flag parts of this add-on as malicious. Specifically, `url_handler.exe`, which opens `remote://` and `tele://` links. If you don't use this feature, you can safely quarantine or delete the file. Otherwise, you must add it as an exception.

SHA256:
