import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


PACKAGE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient"
PACKAGE_NAME = "configuration_test_remote_client"


class Config(dict):
	def write(self):
		self.written = True


def _load_configuration():
	global_vars = ModuleType("globalVars")
	global_vars.appArgs = SimpleNamespace(secure=False, launcher=False, configPath="")
	sys.modules["globalVars"] = global_vars

	configobj = ModuleType("configobj")
	configobj.ConfigObj = object
	configobj.validate = SimpleNamespace(Validator=type("Validator", (), {}))
	sys.modules["configobj"] = configobj

	package = ModuleType(PACKAGE_NAME)
	package.__path__ = [str(PACKAGE_PATH)]
	sys.modules[PACKAGE_NAME] = package
	socket_spec = importlib.util.spec_from_file_location(
		f"{PACKAGE_NAME}.socket_utils",
		PACKAGE_PATH / "socket_utils.py",
	)
	assert socket_spec is not None and socket_spec.loader is not None
	socket_utils = importlib.util.module_from_spec(socket_spec)
	sys.modules[f"{PACKAGE_NAME}.socket_utils"] = socket_utils
	socket_spec.loader.exec_module(socket_utils)

	configuration_spec = importlib.util.spec_from_file_location(
		f"{PACKAGE_NAME}.configuration",
		PACKAGE_PATH / "configuration.py",
	)
	assert configuration_spec is not None and configuration_spec.loader is not None
	configuration = importlib.util.module_from_spec(configuration_spec)
	sys.modules[f"{PACKAGE_NAME}.configuration"] = configuration
	configuration_spec.loader.exec_module(configuration)
	return configuration


configuration = _load_configuration()


def _server_config(**values):
	section = {
		"host": "nvdaremote.accessolutions.fr",
		"port": 443,
		"transport": "tcp",
		"https_path": configuration.DEFAULT_HTTPS_PATH,
		"https_version": "auto",
		"https_path_v2": configuration.DEFAULT_HTTPS_PATH_V2,
	}
	section.update(values)
	return Config(
		controlserver=section,
		connections={"last_connected": []},
	)


def test_configspec_accepts_https_and_declares_default_path():
	spec = configuration.configspec.getvalue()

	assert 'option("tcp", "websocket", "https", default="tcp")' in spec
	assert 'https_path = string(default="/remote/http/v1")' in spec
	assert 'https_version = option("auto", "v1", "v2", default="auto")' in spec
	assert 'https_path_v2 = string(default="/remote/http/v2")' in spec


def test_old_configuration_without_https_path_gets_default():
	config = _server_config()
	del config["controlserver"]["https_path"]
	del config["controlserver"]["https_version"]
	del config["controlserver"]["https_path_v2"]

	assert configuration._migrate_server_configuration(config) is True
	assert config["controlserver"]["https_path"] == configuration.DEFAULT_HTTPS_PATH
	assert config["controlserver"]["https_version"] == "auto"
	assert config["controlserver"]["https_path_v2"] == configuration.DEFAULT_HTTPS_PATH_V2


def test_invalid_https_version_is_reset_to_auto():
	config = _server_config(https_version="unsupported")

	assert configuration._migrate_server_configuration(config) is True
	assert config["controlserver"]["https_version"] == "auto"


def test_https_version_is_normalized_to_lowercase():
	config = _server_config(https_version="V2")

	assert configuration._migrate_server_configuration(config) is True
	assert config["controlserver"]["https_version"] == "v2"


@pytest.mark.parametrize("transport", ("websocket", "https"))
def test_https_transports_keep_known_relay_port_443(transport):
	config = _server_config(transport=transport)

	configuration._migrate_server_configuration(config)

	assert config["controlserver"]["port"] == 443


def test_tcp_migration_still_converts_known_relay_port_443():
	config = _server_config(transport="tcp")

	configuration._migrate_server_configuration(config)

	assert config["controlserver"]["port"] == configuration.socket_utils.SERVER_PORT


def test_empty_https_path_is_normalized():
	config = _server_config(https_path="")

	configuration._migrate_server_configuration(config)

	assert config["controlserver"]["https_path"] == configuration.DEFAULT_HTTPS_PATH


def test_external_import_copies_https_path_alias(monkeypatch):
	target = _server_config(
		port=configuration.socket_utils.SERVER_PORT,
		transport="tcp",
		https_path=configuration.DEFAULT_HTTPS_PATH,
	)
	target["connections"]["last_connected"] = [configuration.DEFAULT_SERVER_HOST]
	source = {
		"controlserver": {
			"host": "nvdaremote.accessolutions.fr",
			"port": 443,
			"transport": "https",
			"httpsPath": "/custom/http/v1",
			"httpsVersion": "v2",
			"httpsPathV2": "/custom/http/v2",
		},
	}

	monkeypatch.setattr(configuration, "readonly", False)
	monkeypatch.setattr(configuration, "_is_portable_copy", lambda: True)
	monkeypatch.setattr(configuration, "get_config", lambda: target)
	monkeypatch.setattr(configuration, "get_legacy_addon_config_paths", lambda: ["legacy.ini"])
	monkeypatch.setattr(configuration, "load_external_config", lambda path: source)

	assert configuration.migrate_external_addon_settings() is True
	assert target["controlserver"]["transport"] == "https"
	assert target["controlserver"]["https_path"] == "/custom/http/v1"
	assert target["controlserver"]["https_version"] == "v2"
	assert target["controlserver"]["https_path_v2"] == "/custom/http/v2"
	assert target["controlserver"]["port"] == 443


def test_https_settings_are_explicit():
	config = _server_config(transport="https")

	assert configuration.has_explicit_remote_settings(config) is True


@pytest.mark.parametrize(
	("transport", "expected"),
	(("tcp", "nvdaremote.accessolutions.fr"), ("https", "nvdaremote.accessolutions.fr:443")),
)
def test_write_connection_migrates_only_tcp_port(monkeypatch, transport, expected):
	config = _server_config()
	monkeypatch.setattr(configuration, "readonly", True)
	monkeypatch.setattr(configuration, "get_config", lambda: config)

	configuration.write_connection_to_config(("nvdaremote.accessolutions.fr", 443), transport)

	assert config["connections"]["last_connected"] == [expected]


def test_refused_websocket_switches_the_saved_relay_to_https(monkeypatch):
	config = _server_config(transport="websocket")
	monkeypatch.setattr(configuration, "readonly", False)
	monkeypatch.setattr(configuration, "get_config", lambda: config)

	assert configuration.switch_saved_transport_to_https(("nvdaremote.accessolutions.fr", 443)) is True
	assert config["controlserver"]["transport"] == "https"
	assert config["controlserver"]["port"] == 443
	assert config.written is True


@pytest.mark.parametrize(
	("values", "address"),
	(
		({"transport": "websocket"}, ("other.example", 443)),
		({"transport": "websocket"}, ("nvdaremote.accessolutions.fr", 8443)),
		({"transport": "websocket", "self_hosted": True}, ("nvdaremote.accessolutions.fr", 443)),
		({"transport": "tcp"}, ("nvdaremote.accessolutions.fr", 443)),
	),
)
def test_refused_websocket_leaves_other_saved_settings_alone(monkeypatch, values, address):
	config = _server_config(**values)
	monkeypatch.setattr(configuration, "get_config", lambda: config)

	assert configuration.switch_saved_transport_to_https(address) is False
	assert config["controlserver"]["transport"] == values["transport"]


@pytest.mark.parametrize("new_transport", ("websocket", "https"))
def test_tcp_fallback_switches_the_saved_relay_and_its_port(monkeypatch, new_transport):
	config = _server_config(transport="tcp", port=configuration.socket_utils.SERVER_PORT)
	monkeypatch.setattr(configuration, "readonly", False)
	monkeypatch.setattr(configuration, "get_config", lambda: config)

	switched = configuration.switch_saved_transport_after_tcp_fallback(
		("nvdaremote.accessolutions.fr", 443),
		new_transport,
	)

	assert switched is True
	assert config["controlserver"]["transport"] == new_transport
	assert config["controlserver"]["port"] == 443
	assert config.written is True


@pytest.mark.parametrize(
	("values", "address", "new_transport"),
	(
		({"transport": "websocket"}, ("nvdaremote.accessolutions.fr", 443), "https"),
		({"transport": "tcp", "self_hosted": True}, ("nvdaremote.accessolutions.fr", 443), "websocket"),
		({"transport": "tcp"}, ("other.example", 443), "websocket"),
		({"transport": "tcp"}, ("nvdaremote.accessolutions.fr", 443), "tcp"),
	),
)
def test_tcp_fallback_leaves_other_saved_settings_alone(monkeypatch, values, address, new_transport):
	config = _server_config(port=configuration.socket_utils.SERVER_PORT, **values)
	monkeypatch.setattr(configuration, "get_config", lambda: config)

	assert configuration.switch_saved_transport_after_tcp_fallback(address, new_transport) is False
	assert config["controlserver"]["transport"] == values["transport"]


def test_configspec_declares_the_proxy_memory_section():
	assert "[proxy_memory]" in configuration.configspec.getvalue()
