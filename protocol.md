# NVDA Remote Protocol Documentation

## Overview

The NVDA Remote protocol facilitates communication between two NVDA instances, enabling remote assistance and collaboration. It uses a client-server model where either client can act as the controlling (master) or controlled (slave) machine.

## Connection Establishment

1. Clients connect to a relay server using one of the supported transports (see below).
2. Clients authenticate by joining a shared channel.
3. The relay server facilitates message passing between connected clients.

## Transport Layer

The same JSON message protocol can be carried over two interchangeable transports. A single relay server can expose both at the same time (on separate ports), and clients on different transports interoperate transparently on the same channel.

### Raw TLS (default)

- A plain TCP connection wrapped in SSL/TLS (historically on port `6837`).
- Each JSON message is terminated with a newline character (`'\n'`).
- This is the original NVDA Remote transport and remains the default.

### WebSocket over HTTPS

- A `wss://` (TLS) WebSocket connection, typically served on port `443` so it can traverse restrictive corporate HTTP proxies.
- The client negotiates the `nvdaremote/2.0` WebSocket subprotocol during the HTTP upgrade handshake.
- Each JSON message is sent as a single WebSocket text frame. The newline terminator is optional and is trimmed by both ends if present.
- HTTP `CONNECT` proxies (with optional Basic authentication or system proxy settings) are supported for outbound connections.
- The application-layer AES-GCM encryption (`encrypted` messages) is applied on top of the WebSocket transport exactly as with raw TLS.

### Proxy support (WebSocket transport)

The WebSocket transport can reach the relay through several proxy types, selected by the `proxy_type` setting:

- `http` — HTTP `CONNECT` proxy (anonymous or Basic authentication).
- `socks4`, `socks4a`, `socks5`, `socks5h` — SOCKS proxies. The `a`/`h` variants perform DNS resolution on the proxy side.
- `negotiate`, `ntlm` — Windows Integrated Authentication proxies. The tunnel is authenticated with the credentials of the current Windows session via SSPI (single sign-on, no password stored).

When no explicit proxy is configured, the Windows system proxy is used automatically.

#### TLS-inspecting proxies (SSL inspection)

Some corporate proxies terminate and re-encrypt TLS, presenting their own certificate instead of the relay's:

- If the proxy's certificate authority is trusted by the Windows certificate store (e.g. deployed by group policy), the connection is verified and established automatically, because certificate validation loads the Windows system certificate store.
- Otherwise the certificate is unverifiable. In that case the client retrieves the certificate fingerprint **through the proxy tunnel** (so it reflects the certificate actually presented on the wire) and prompts the user to trust it. Once trusted, the fingerprint is remembered for subsequent connections.

## Message Format

Messages are serialized as JSON objects with a 'type' field indicating the message type. Over raw TLS each message is terminated with a newline character (`'\n'`); over WebSocket each message is one text frame.

## Protocol Version Negotiation

1. Upon connection, the client sends a `protocol_version` message.
2. If versions are incompatible, an error is sent and the connection is closed.

## Message Types

Below is a detailed specification of each message type using JSONSchema:

### Connection Setup

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "definitions": {
    "protocol_version": {
      "type": "object",
      "properties": {
        "type": { "const": "protocol_version" },
        "version": { "type": "integer" }
      },
      "required": ["type", "version"]
    },
    "join": {
      "type": "object",
      "properties": {
        "type": { "const": "join" },
        "channel": { "type": "string" },
        "connection_type": { "enum": ["master", "slave"] }
      },
      "required": ["type", "channel", "connection_type"]
    },
    "channel_joined": {
      "type": "object",
      "properties": {
        "type": { "const": "channel_joined" },
        "channel": { "type": "string" },
        "clients": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "id": { "type": "integer" },
              "connection_type": { "enum": ["master", "slave"] }
            },
            "required": ["id", "connection_type"]
          }
        }
      },
      "required": ["type", "channel", "clients"]
    },
    "client_joined": {
      "type": "object",
      "properties": {
        "type": { "const": "client_joined" },
        "client": {
          "type": "object",
          "properties": {
            "id": { "type": "integer" },
            "connection_type": { "enum": ["master", "slave"] }
          },
          "required": ["id", "connection_type"]
        }
      },
      "required": ["type", "client"]
    },
    "client_left": {
      "type": "object",
      "properties": {
        "type": { "const": "client_left" },
        "client": { "type": "integer" }
      },
      "required": ["type", "client"]
    }
  }
}
```

### Control Messages

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "definitions": {
    "key": {
      "type": "object",
      "properties": {
        "type": { "const": "key" },
        "vk_code": { "type": "integer" },
        "scan_code": { "type": "integer" },
        "extended": { "type": "boolean" },
        "pressed": { "type": "boolean" }
      },
      "required": ["type", "vk_code", "scan_code", "extended", "pressed"]
    },
    "speak": {
      "type": "object",
      "properties": {
        "type": { "const": "speak" },
        "sequence": {
          "type": "array",
          "items": {
            "oneOf": [
              { "type": "string" },
              {
                "type": "array",
                "items": [
                  { "type": "string" },
                  { "type": "object" }
                ],
                "minItems": 2,
                "maxItems": 2
              }
            ]
          }
        },
        "priority": { "type": "string" }
      },
      "required": ["type", "sequence", "priority"]
    },
    "cancel": {
      "type": "object",
      "properties": {
        "type": { "const": "cancel" }
      },
      "required": ["type"]
    },
    "pause_speech": {
      "type": "object",
      "properties": {
        "type": { "const": "pause_speech" },
        "switch": { "type": "boolean" }
      },
      "required": ["type", "switch"]
    },
    "tone": {
      "type": "object",
      "properties": {
        "type": { "const": "tone" },
        "hz": { "type": "number" },
        "length": { "type": "number" },
        "left": { "type": "number" },
        "right": { "type": "number" }
      },
      "required": ["type", "hz", "length", "left", "right"]
    },
    "wave": {
      "type": "object",
      "properties": {
        "type": { "const": "wave" },
        "fileName": { "type": "string" },
        "asynchronous": { "type": "boolean" }
      },
      "required": ["type", "fileName"]
    },
    "display": {
      "type": "object",
      "properties": {
        "type": { "const": "display" },
        "cells": { "type": "array", "items": { "type": "integer" } }
      },
      "required": ["type", "cells"]
    },
    "braille_input": {
      "type": "object",
      "properties": {
        "type": { "const": "braille_input" },
        "dots": { "type": "integer" },
        "space": { "type": "boolean" },
        "routingIndex": { "type": "integer" }
      },
      "required": ["type"]
    },
    "set_clipboard_text": {
      "type": "object",
      "properties": {
        "type": { "const": "set_clipboard_text" },
        "text": { "type": "string" }
      },
      "required": ["type", "text"]
    },
    "send_SAS": {
      "type": "object",
      "properties": {
        "type": { "const": "send_SAS" }
      },
      "required": ["type"]
    },
    "request_screenshot": {
      "type": "object",
      "properties": {
        "type": { "const": "request_screenshot" }
      },
      "required": ["type"]
    },
    "screenshot": {
      "type": "object",
      "properties": {
        "type": { "const": "screenshot" },
        "content": { "type": "string", "description": "Base64-encoded image data of the controlled machine's screen." },
        "format": { "type": "string", "description": "Image format of the encoded content, e.g. 'jpg'." }
      },
      "required": ["type", "content"]
    }
  }
}
```

The screenshot feature always transfers an image from the controlled (slave)
machine to the controlling (master) machine, but it can be triggered from either
end:

- The controlling machine sends `request_screenshot`. The controlled machine
  captures its screen, scales it down to a lower (but still readable) resolution,
  and returns it as a `screenshot` message.
- The controlled machine can also push its screen directly by sending a
  `screenshot` message without a preceding request.

Upon receiving a `screenshot` message, the controlling machine writes the
decoded image to a temporary file and opens it with the system's default image
viewer.

### File Transfer

Small files may use the legacy `file_transfer` message. Larger files use a
streaming transfer made of ordered messages:

1. `file_transfer_start` announces the file name, total size and chunk size.
2. `file_transfer_chunk` carries one Base64-encoded chunk and its index.
3. `file_transfer_complete` carries the total size and SHA-256 checksum.
4. The receiver answers each stage with `file_transfer_ack`. The sender waits
  for each acknowledgement before sending the next chunk, keeping memory use
  bounded and providing backpressure.

An interrupted transfer is announced with `file_transfer_abort`. The receiver
writes incoming data to a temporary file and moves it to the selected path only
after the size and checksum have been verified. There is no fixed maximum size
for the chunked format; the practical limits are available disk space, network
reliability and transfer timeouts.

### Braille Support

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "definitions": {
    "set_braille_info": {
      "type": "object",
      "properties": {
        "type": { "const": "set_braille_info" },
        "name": { "type": "string" },
        "numCells": { "type": "integer" }
      },
      "required": ["type", "name", "numCells"]
    },
    "set_display_size": {
      "type": "object",
      "properties": {
        "type": { "const": "set_display_size" },
        "sizes": { "type": "array", "items": { "type": "integer" } }
      },
      "required": ["type", "sizes"]
    }
  }
}
```

### Miscellaneous

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "definitions": {
    "ping": {
      "type": "object",
      "properties": {
        "type": { "const": "ping" }
      },
      "required": ["type"]
    },
    "error": {
      "type": "object",
      "properties": {
        "type": { "const": "error" },
        "message": { "type": "string" }
      },
      "required": ["type", "message"]
    }
  }
}
```

## Security Considerations

- All connections are encrypted using SSL/TLS.
- Clients can verify the server's certificate fingerprint to prevent man-in-the-middle attacks.
- The channel key acts as a shared secret for authentication.

## Error Handling

- Connection errors trigger automatic reconnection attempts.
- Protocol errors are communicated using the `error` message type.

This protocol documentation provides a high-level overview of the NVDA Remote functionality. For detailed implementation, refer to the source code files, particularly `transport.py`, `session.py`, and `serializer.py`.
