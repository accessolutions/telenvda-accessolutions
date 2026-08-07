[[!meta title="Telenvda by Accessolutions"]]

# Telenvda by Accessolutions

TeleNVDA is an NVDA add-on for remote assistance. It allows a trusted person
to control an NVDA computer, or to provide access to another computer running
NVDA. This project is maintained by Accessolutions and remains compatible with
the NVDA Remote protocol where the relay server supports it.

The project includes work from the NVDA Spanish community and other
contributors. Original work includes contributions by Tyler Spivey and
Christopher Toth. It is distributed under the GNU General Public License,
version 2 or later.

## Main features

* TCP/TLS connections through the traditional NVDA Remote protocol.
* Secure WebSocket connections (`wss://`) through HTTPS-compatible relays.
* WebSocket subprotocol `nvdaremote/2.0`, with port 443 as the usual choice.
* Manual proxy settings, automatic Windows proxy detection (WinHTTP, PAC/WPAD
  and bypass rules), or an explicit no-proxy mode.
* Optional AES-GCM application-layer encryption for compatible TeleNVDA peers.
* Direct server mode for connections that do not use a relay.
* File transfer, clipboard sharing, remote speech, braille, and secure-desktop support.
* A connectivity test that can check a controller using TCP or WebSocket.
* Two remote screenshot workflows described below.

## Installation

Install the `.nvda-addon` package through NVDA's Add-ons Manager. NVDA must
be installed on every participating computer. Restart NVDA if it requests a
restart after installation or an update.

For secure-desktop access, install the add-on on the secure desktop through
NVDA's General Settings, using **Use currently saved settings on the logon and
other secure screens**. This requires administrator privileges.

## Updates

TeleNVDA can check the public GitHub Releases repository when NVDA starts.
Open **NVDA menu > Tools > Remote > Options** to enable or disable this check
and to choose **Stable releases** or **Development releases**. A manual check
is also available through **NVDA menu > Tools > Remote > Check for updates**.

An update is never installed silently. TeleNVDA asks for confirmation, downloads
the `.nvda-addon` package over HTTPS, verifies its published SHA-256 hash, and
then asks whether NVDA should be restarted. The update check uses the proxy
configured for TeleNVDA, including HTTP, SOCKS, `negotiate`, and `ntlm` proxy
types. Automatic network errors are only written to the log; manual errors are
shown to the user.

## Relay connections

A relay connection is the recommended choice when the computers are behind
routers or restrictive firewalls.

### Computer to be controlled

1. Open **NVDA menu > Tools > Remote > Connect**.
2. Select **Client** and **Allow this machine to be controlled**.
3. Enter the relay host and access key. The controlled computer and controller
   must use the same key.
4. Optionally enter an AES-GCM encryption password. Every TeleNVDA participant
   must use the same password; this option is not compatible with all NVDA
   Remote clients.
5. Select **WebSocket over HTTPS** when the relay provides WebSocket support.
   Use port **443** unless the relay administrator specifies another port.
   The WebSocket path is normally `/` and can be changed in the add-on options.
6. Press **OK**.

### Controlling computer

Use the same connection dialog, select **Client**, and choose **Control another
machine**. Enter the same relay host, protocol, port, WebSocket path, access
key, and optional encryption password.

For a WebSocket connection, TeleNVDA uses `wss://` and the
`nvdaremote/2.0` subprotocol. This makes the traffic resemble ordinary HTTPS
traffic while retaining the NVDA Remote session protocol.

### Proxies and certificate warnings

Open **NVDA menu > Tools > Remote > Options** to configure the proxy mode and,
when using manual configuration, an HTTP or SOCKS proxy. Manual configuration
is the default and preserves the historical behavior: if no proxy host is
entered, the network libraries may use proxy environment variables. Automatic
Windows proxy detection follows the current user's WinHTTP configuration,
including PAC/WPAD scripts and destination bypass rules, without extracting or
storing the Windows password. No proxy ignores proxy environment variables.
HTTP, SOCKS4/4a, and SOCKS5/5h are supported. For WebSocket relay
connections, **negotiate** uses the Windows SSPI provider and can select
Kerberos or NTLM, while **ntlm** forces NTLM authentication. Leaving the
proxy username empty uses the current Windows session; explicit credentials
can be entered as `DOMAIN\\user` and a password. These two SSPI modes apply to
the HTTP proxy CONNECT handshake, not to the NVDA Remote relay itself.

The direct TCP/TLS server connection does not use the WebSocket proxy path.
Use WebSocket over HTTPS when an enterprise HTTP proxy must be traversed.

TLS certificates are verified. If a relay uses a certificate that Windows does
not recognize, TeleNVDA automatically accepts the certificate and saves its
fingerprint so that manual and automatic connections are not blocked by a
dialog. Verify the expected fingerprint with the relay administrator before
the first connection. Saved fingerprints can be removed with **Delete all
trusted fingerprints** in the options.

## Direct server mode

The **Server** option in the connection dialog starts a local direct server.
The other participant connects to the external address and port shown by the
server. Port 6837 is the default; Windows Firewall and router port forwarding
may be required. UPnP forwarding is available when the router supports it.

The direct server uses TLS and creates a unique self-signed certificate in the
NVDA user configuration directory on first use. The private key is not part of
the add-on source or package. Never copy a generated `teleNVDA-server.pem`
file into this repository or share it publicly.

Direct Server mode intentionally listens on the classic TCP/TLS protocol.
WebSocket is a relay transport; selecting WebSocket does not turn the local
direct server into a WebSocket server.

Use a long, randomly generated access key. The access key is an authentication
secret and must not be put in source code, issue reports, screenshots, or logs.

## Remote screenshots

The Remote menu contains two distinct commands:

* **Remote screenshot** uses the native TeleNVDA screenshot messages. It is
  the preferred method when TeleNVDA is installed on the controlled computer.
  Default gesture: **NVDA+Control+Shift+P**.
* **Request screenshot (PowerShell)** runs a temporary Windows PowerShell
  capture on the controlled computer and transfers the resulting PNG through
  the TeleNVDA session. Default gesture: **Windows+Alt+P**.

Both gestures work from either end of the session. On the controlling computer
they request a capture from the controlled computer; on the controlled computer
they capture the local screen and push it to the controller.

The beta workflow does not install or require the separate Python screenshot
helper. PowerShell must be available on the controlled Windows computer.

A screenshot is received as image data and opened on the controlling computer.
The capture is converted to JPEG before being Base64 encoded, so that it
transfers quickly. It is saved in the folder configured in the add-on options,
or in the user's temporary folder when no folder is configured.
Treat screenshots as potentially sensitive information and share them only
with authorized people.

## Controlling the remote computer

Press **NVDA+Alt+Tab** (Insert+Alt+Tab with the default NVDA key) to switch
between controlling the local and remote computer. When remote control is
active, keyboard and braille input are sent to the controlled computer. The
gesture can be changed in NVDA's Input Gestures dialog. On a controlled
computer, the same gesture requests that the controller return control to the
local machine. For best results, use matching keyboard layouts on both
computers.

On the controlling computer, holding the right mouse button down for five
seconds restarts NVDA without confirmation.

The Remote menu also provides commands for sending Ctrl+Alt+Delete, muting
remote speech, pushing clipboard text, and sending files. File transfers are
available to session members and should only be used with trusted peers.

## Connectivity testing

Open **NVDA menu > Tools > Remote > Connectivity test**. Enter the relay
address, protocol, port, and WebSocket path when applicable. The test records
DNS, TLS, and WebSocket diagnostic information in the local connectivity log;
it does not require or record a session access key.

## Security recommendations

* Use relay servers and hosts that you trust.
* Verify TLS fingerprints out of band before the first connection to a relay
  whose certificate is not recognized by Windows.
* Use unique, high-entropy access and encryption keys and rotate them if they
  may have been exposed.
* Do not commit private keys, passwords, access keys, screenshots, or generated
  `.nvda-addon` files to source control.
* Keep the add-on and NVDA updated on every participating computer.
* Direct server mode is intended for trusted, controlled environments; expose
  its port only when necessary.

## Building from source

This repository targets Python 3.13 and uses SCons. Install the dependencies
from `pyproject.toml`, then run `scons` from the repository root. The generated
`.nvda-addon` file is written to the root directory and is intentionally
ignored by Git.

The build copies this file into the English documentation directory and
converts the translated Markdown files to HTML. Keep the root `readme.md` as
the English source instead of editing generated files under `addon/doc/en/`.

## Repository

Source code and issue tracking are available at:

<https://github.com/Accessolutions/telenvda-accessolutions>

[[!tag dev stable]]
