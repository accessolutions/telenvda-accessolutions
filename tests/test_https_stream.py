import importlib.util
import io
from pathlib import Path
import sys
from types import ModuleType

import pytest


PACKAGE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient"
PACKAGE_NAME = "https_stream_test_package"


def _load_module(name: str, path: Path) -> ModuleType:
	spec = importlib.util.spec_from_file_location(name, path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[name] = module
	spec.loader.exec_module(module)
	return module


package = ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
sys.modules[PACKAGE_NAME] = package
https_protocol = _load_module(
	f"{PACKAGE_NAME}.https_protocol",
	PACKAGE_PATH / "https_protocol.py",
)
https_stream = _load_module(
	f"{PACKAGE_NAME}.https_stream",
	PACKAGE_PATH / "https_stream.py",
)


def test_read_event_returns_message_and_decodes_payload():
	response = io.BytesIO(b"id: 7\ndata: {\"type\":\"ping\"}\n\n")

	event = https_stream.read_event(response, 4096)

	assert event == https_stream.StreamEvent("message", '{"type":"ping"}', 7)
	assert https_stream.decode_event_payload(event) == {"type": "ping"}


def test_read_event_returns_ready_event():
	response = io.BytesIO(b"event: ready\ndata: {}\n\n")

	event = https_stream.read_event(response, 4096)

	assert event is not None
	assert event.event == "ready"
	assert event.data == "{}"
	assert event.sequence is None


def test_read_event_joins_multiple_data_lines():
	response = io.BytesIO(b"event: update\nid: 8\ndata: first\ndata: second\n\n")

	event = https_stream.read_event(response, 4096)

	assert event == https_stream.StreamEvent("update", "first\nsecond", 8)


def test_read_event_ignores_keepalive():
	response = io.BytesIO(b": keepalive\n\n")

	assert https_stream.read_event(response, 4096) is None


@pytest.mark.parametrize("line", (b"id: nope\n", b"id: 0\n", b"id: -1\n"))
def test_read_event_rejects_invalid_sequence(line):
	with pytest.raises(https_protocol.HTTPSProtocolError) as error:
		https_stream.read_event(io.BytesIO(line), 4096)

	assert error.value.reason == "invalid_sequence"


def test_read_event_rejects_oversized_payload_and_line():
	with pytest.raises(https_protocol.HTTPSProtocolError) as payload_error:
		https_stream.read_event(io.BytesIO(b"data: 1234\n\n"), 3)
	assert payload_error.value.reason == "invalid_stream"

	long_line = b"x" * https_protocol.MAX_STREAM_LINE_BYTES + b"\n\n"
	with pytest.raises(https_protocol.HTTPSProtocolError) as line_error:
		https_stream.read_event(io.BytesIO(long_line), 4096)
	assert line_error.value.reason == "invalid_stream"


def test_read_event_rejects_truncated_and_invalid_utf8_blocks():
	with pytest.raises(https_stream.HTTPSStreamClosed):
		https_stream.read_event(io.BytesIO(b"data: {\"type\":\"ping\"}"), 4096)

	with pytest.raises(https_protocol.HTTPSProtocolError) as error:
		https_stream.read_event(io.BytesIO(b"data: \xff\n\n"), 4096)
	assert error.value.reason == "invalid_stream"


def test_decode_event_payload_rejects_invalid_json_and_non_object():
	invalid_json = https_stream.StreamEvent("message", "not-json", None)
	with pytest.raises(https_protocol.HTTPSProtocolError) as json_error:
		https_stream.decode_event_payload(invalid_json)
	assert json_error.value.reason == "invalid_json"

	with pytest.raises(https_protocol.HTTPSProtocolError) as type_error:
		https_stream.decode_event_payload(https_stream.StreamEvent("message", "[]", None))
	assert type_error.value.reason == "invalid_message"
