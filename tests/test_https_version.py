import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / "addon/globalPlugins/remoteClient/https_version.py"
SPEC = importlib.util.spec_from_file_location("https_version", MODULE_PATH)
https_version = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(https_version)


@pytest.fixture(autouse=True)
def clear_version_memory():
	https_version.forget_all()
	yield
	https_version.forget_all()


def test_auto_prefers_v2_until_a_route_failure_is_remembered():
	key = https_version.network_key(("relay.example", 443), None)

	assert https_version.versions_to_try(https_version.AUTO, key) == [https_version.V2, https_version.V1]
	https_version.remember_failure(key)
	assert https_version.versions_to_try(https_version.AUTO, key) == [https_version.V1]
	https_version.remember_success(key)
	assert https_version.versions_to_try(https_version.AUTO, key) == [https_version.V2, https_version.V1]


@pytest.mark.parametrize("preference, expected", ((https_version.V1, [https_version.V1]), (https_version.V2, [https_version.V2])))
def test_forced_version_ignores_route_memory(preference, expected):
	key = https_version.network_key(("relay.example", 443), None)
	https_version.remember_failure(key)

	assert https_version.versions_to_try(preference, key) == expected


def test_proxy_is_part_of_the_route_key():
	address = ("relay.example", 443)
	first_proxy = SimpleNamespace(host="proxy.office", port=8080)
	second_proxy = SimpleNamespace(host="proxy.home", port=8080)

	first_key = https_version.network_key(address, first_proxy)
	second_key = https_version.network_key(address, second_proxy)

	assert first_key != second_key
	https_version.remember_failure(first_key)
	assert https_version.versions_to_try(https_version.AUTO, first_key) == [https_version.V1]
	assert https_version.versions_to_try(https_version.AUTO, second_key) == [https_version.V2, https_version.V1]