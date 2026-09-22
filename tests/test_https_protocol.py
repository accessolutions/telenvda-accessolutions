import base64
import importlib.util
import json
from pathlib import Path
import sys

import pytest


MODULE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient/https_protocol.py"
SPEC = importlib.util.spec_from_file_location("https_protocol", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
https_protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = https_protocol
SPEC.loader.exec_module(https_protocol)


def _session_id() -> str:
	return base64.urlsafe_b64encode(bytes(range(16))).decode("ascii").rstrip("=")


def test_paths_are_normalized_and_urls_support_ipv6():
	assert https_protocol.normalize_https_path("") == "/remote/http/v1"
	assert https_protocol.normalize_https_path("remote/http/v1/") == "/remote/http/v1"
	assert https_protocol.normalize_https_path("/") == "/"
	assert https_protocol.https_url("2001:db8::1", 443) == "https://[2001:db8::1]:443/remote/http/v1"

	for invalid in ("/remote/http/v1?query", "/remote\\http", "/remote/http\n/v1"):
		with pytest.raises(https_protocol.HTTPSProtocolError):
			https_protocol.normalize_https_path(invalid)


def test_session_identifier_requires_unpadded_base64url_with_sufficient_entropy():
	session_id = _session_id()

	assert https_protocol.validate_session_id(session_id) == session_id

	for invalid in ("short", session_id + "=", session_id + "/", "é" + session_id):
		with pytest.raises(https_protocol.HTTPSProtocolError):
			https_protocol.validate_session_id(invalid)


def test_session_response_and_message_envelopes_round_trip_as_json():
	response = https_protocol.decode_session_response(
		json.dumps(
			{
				"sessionId": _session_id(),
				"token": base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
				"protocol": "nvdaremote-http/1",
				"pollWaitSeconds": 25,
				"maxBatchMessages": 32,
				"maxMessageBytes": 1048576,
			},
		).encode("utf-8"),
	)
	assert response.protocol == "nvdaremote-http/1"

	body = https_protocol.encode_message_batch(
		[
			https_protocol.MessageEnvelope(1, {"type": "protocol_version", "version": 2}),
			https_protocol.MessageEnvelope(
				2,
				{"type": "join", "channel": "1234567", "connection_type": "master"},
			),
		],
	)
	assert json.loads(body) == {
		"messages": [
			{"sequence": 1, "payload": {"type": "protocol_version", "version": 2}},
			{"sequence": 2, "payload": {"type": "join", "channel": "1234567", "connection_type": "master"}},
		],
	}

	poll = https_protocol.decode_poll_response(
		json.dumps(
			{
				"messages": [{"sequence": 3, "payload": {"type": "ping"}}],
				"lastSequence": 3,
			},
		).encode("utf-8"),
	)
	assert poll.last_sequence == 3
	assert poll.messages == (https_protocol.MessageEnvelope(3, {"type": "ping"}),)


def test_decoders_reject_malformed_json_missing_fields_and_wrong_types():
	with pytest.raises(https_protocol.HTTPSProtocolError):
		https_protocol.decode_poll_response(b"not-json")
	with pytest.raises(https_protocol.HTTPSProtocolError):
		https_protocol.decode_session_response(json.dumps({}).encode("utf-8"))
	with pytest.raises(https_protocol.HTTPSProtocolError):
		https_protocol.decode_poll_response(json.dumps({"messages": {}, "lastSequence": 0}).encode("utf-8"))
	with pytest.raises(https_protocol.HTTPSProtocolError):
		https_protocol.decode_poll_response(
			json.dumps(
				{
					"messages": [{"sequence": 1, "payload": []}],
					"lastSequence": 1,
				},
			).encode("utf-8"),
		)


def test_sequence_tracker_accepts_order_ignores_duplicates_and_rejects_gaps():
	tracker = https_protocol.SequenceTracker()

	assert tracker.accept(1) is True
	assert tracker.accept(1) is False
	with pytest.raises(https_protocol.HTTPSSequenceError) as error:
		tracker.accept(3)
	assert error.value.expected == 2
	assert error.value.received == 3
