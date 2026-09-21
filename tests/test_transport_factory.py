import importlib.util
import builtins
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


PACKAGE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient"
PLUGIN_PATH = PACKAGE_PATH / "__init__.py"
MODULE_NAME = "remote_client_factory_test"


def _module(name, **attributes):
	module = ModuleType(name)
	for attribute, value in attributes.items():
		setattr(module, attribute, value)
	return module


def _install_stubs():
	builtins._ = lambda value, *args, **kwargs: value
	def script(*args, **kwargs):
		return lambda function: function

	addon_handler = _module(
		"addonHandler",
		AddonError=Exception,
		getAvailableAddons=lambda: [],
		initTranslation=lambda: None,
	)
	global_vars = _module("globalVars", appArgs=SimpleNamespace(secure=False, launcher=False))
	gui = _module("gui", NVDASettingsDialog=type("NVDASettingsDialog", (), {}))
	gui.mainFrame = SimpleNamespace(sysTrayIcon=SimpleNamespace(toolsMenu=None))
	config = _module("config", conf={"keyboard": {"handleInjectedKeys": True}})
	global_plugin_handler = _module("globalPluginHandler", GlobalPlugin=type("BasePlugin", (), {}))
	keyboard_handler = _module("keyboardHandler", KeyboardInputGesture=object)
	log_handler = _module("logHandler", log=SimpleNamespace())
	script_handler = _module("scriptHandler", script=script)
	win_user = _module("winUser", WM_QUIT=0, VK_NONE=0xFF)
	build_version = _module("buildVersion", version_year=2026)
	wx = _module("wx", CallAfter=lambda function, *args, **kwargs: function(*args, **kwargs))
	wx.__getattr__ = lambda name: 0

	for name, module in {
		"addonHandler": addon_handler,
		"api": _module("api"),
		"configobj": _module("configobj"),
		"core": _module("core"),
		"braille": _module("braille"),
		"globalVars": global_vars,
		"gui": gui,
		"IAccessibleHandler": _module("IAccessibleHandler"),
		"queueHandler": _module("queueHandler"),
		"buildVersion": build_version,
		"shlobj": _module("shlobj"),
		"speech": _module("speech"),
		"ui": _module("ui"),
		"wx": wx,
		"config": config,
		"globalPluginHandler": global_plugin_handler,
		"keyboardHandler": keyboard_handler,
		"logHandler": log_handler,
		"scriptHandler": script_handler,
		"winUser": win_user,
	}.items():
		sys.modules.setdefault(name, module)

	secure_desktop = _module("winAPI.secureDesktop", post_secureDesktopStateChange=lambda: None)
	win_api = _module("winAPI")
	win_api.__path__ = []
	sys.modules.setdefault("winAPI", win_api)
	sys.modules.setdefault("winAPI.secureDesktop", secure_desktop)
	sys.modules.setdefault("utils", _module("utils"))
	sys.modules.setdefault("utils.security", _module("utils.security"))
	input_core = _module("inputCore")
	sys.modules.setdefault("inputCore", input_core)

	package = ModuleType(MODULE_NAME)
	package.__path__ = [str(PACKAGE_PATH)]
	sys.modules[MODULE_NAME] = package

	for name in (
		"bridge",
		"configuration",
		"cues",
		"dialogs",
		"keep_awake",
		"local_machine",
		"mouse_hook",
		"remote_keyboard",
		"serializer",
		"server",
		"updater",
		"url_handler",
	):
		sys.modules.setdefault("%s.%s" % (MODULE_NAME, name), _module("%s.%s" % (MODULE_NAME, name)))
	sys.modules["%s.serializer" % MODULE_NAME] = _module(
		"%s.serializer" % MODULE_NAME,
		JSONSerializer=type("JSONSerializer", (), {}),
	)

	socket_utils = _module(
		"%s.socket_utils" % MODULE_NAME,
		SERVER_PORT=6837,
		address_to_hostport=lambda address, default_port=None: address,
		wrap_socket=lambda socket, *args, **kwargs: socket,
	)
	sys.modules["%s.socket_utils" % MODULE_NAME] = socket_utils

	https_protocol = _module(
		"%s.https_protocol" % MODULE_NAME,
		DEFAULT_PATH="/remote/http/v1",
		DEFAULT_PATH_V2="/remote/http/v2",
	)
	sys.modules["%s.https_protocol" % MODULE_NAME] = https_protocol

	class FakeRelayTransport:
		def __init__(self, **kwargs):
			self.kwargs = kwargs

	class FakeWebSocketRelayTransport(FakeRelayTransport):
		pass

	class FakeHTTPSRelayTransport(FakeRelayTransport):
		pass

	def create_https_relay_transport(**kwargs):
		return FakeHTTPSRelayTransport(**kwargs)

	transport = _module(
		"%s.transport" % MODULE_NAME,
		RelayTransport=FakeRelayTransport,
		WebSocketRelayTransport=FakeWebSocketRelayTransport,
		HTTPSRelayTransport=FakeHTTPSRelayTransport,
		HTTPSStreamRelayTransport=FakeHTTPSRelayTransport,
		create_https_relay_transport=create_https_relay_transport,
		TransportEvents=SimpleNamespace(),
	)
	sys.modules["%s.transport" % MODULE_NAME] = transport

	session = _module(
		"%s.session" % MODULE_NAME,
		MasterSession=type("MasterSession", (), {}),
		SlaveSession=type("SlaveSession", (), {}),
	)
	sys.modules["%s.session" % MODULE_NAME] = session


def _load_plugin():
	_install_stubs()
	spec = importlib.util.spec_from_file_location(
		MODULE_NAME,
		PLUGIN_PATH,
		submodule_search_locations=[str(PACKAGE_PATH)],
	)
	assert spec is not None and spec.loader is not None
	plugin = importlib.util.module_from_spec(spec)
	sys.modules[MODULE_NAME] = plugin
	spec.loader.exec_module(plugin)
	return plugin


@pytest.fixture(scope="module")
def plugin():
	return _load_plugin()


@pytest.mark.parametrize(
	("transport_type", "transport_name"),
	(("tcp", "RelayTransport"), ("websocket", "WebSocketRelayTransport"), ("https", "HTTPSRelayTransport")),
)
def test_factory_selects_explicit_transport(plugin, transport_type, transport_name):
	owner = object.__new__(plugin.GlobalPlugin)
	transport = owner._create_relay_transport(
		("example.com", 443),
		"1234567",
		None,
		"master",
		transport_type=transport_type,
		https_path="/custom/http/v1",
		https_preference="v2",
		https_path_v2="/custom/http/v2",
	)

	expected_class = getattr(plugin, transport_name)
	assert isinstance(transport, expected_class)
	if transport_type == "https":
		assert transport.kwargs["https_path"] == "/custom/http/v1"
		assert transport.kwargs["preference"] == "v2"
		assert transport.kwargs["https_path_v2"] == "/custom/http/v2"
	elif transport_type == "websocket":
		assert "https_path" not in transport.kwargs
		assert transport.kwargs["ws_path"] == "/"
	else:
		assert "https_path" not in transport.kwargs
		assert "ws_path" not in transport.kwargs


def test_factory_rejects_unknown_transport(plugin):
	owner = object.__new__(plugin.GlobalPlugin)

	with pytest.raises(ValueError, match="Unknown transport type"):
		owner._create_relay_transport(
			("example.com", 443),
			"1234567",
			None,
			"master",
			transport_type="invalid",
		)