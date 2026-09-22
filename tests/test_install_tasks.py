import builtins
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / "addon/installTasks.py"
MODULE_NAME = "telenvda_install_tasks"


class Addon:
	"""The little of an NVDA add-on which the installation tasks rely on."""

	def __init__(self, name, path, removable=True):
		self.name = name
		self.path = str(path)
		self.isDisabled = False
		self.removable = removable
		self.remove_requested = False

	def requestRemove(self):
		if not self.removable:
			raise RuntimeError("the add-on is loaded")
		self.remove_requested = True


def _nothing(*args, **kwargs):
	return None


def _module(name, **attributes):
	module = ModuleType(name)
	for attribute, value in attributes.items():
		setattr(module, attribute, value)
	return module


def _load_install_tasks():
	"""Load the installation tasks against fake NVDA modules of our own.

	The stubs are removed from the module registry once the module is loaded: the
	other test modules of this suite install their own, and the installation tasks
	keep a reference to the ones they were given here.
	"""
	builtins._ = lambda message, *args, **kwargs: message
	stubs = {
		"addonHandler": _module(
			"addonHandler",
			AddonError=type("AddonError", (Exception,), {}),
			DELETEDIR_SUFFIX=".delete",
			initTranslation=_nothing,
			getAvailableAddons=lambda: [],
		),
		"buildVersion": _module("buildVersion", version_year=2026),
		"config": _module("config", conf={}),
		"configobj": _module("configobj", ConfigObj=dict),
		"globalVars": _module(
			"globalVars",
			appArgs=SimpleNamespace(secure=False, launcher=False, minimal=False, configPath=""),
		),
		"gui": _module("gui", messageBox=_nothing, mainFrame=None),
		"logHandler": _module(
			"logHandler",
			log=SimpleNamespace(info=_nothing, warning=_nothing, exception=_nothing),
		),
		"wx": _module("wx", YES=1, NO=0, OK=1, YES_NO=1, ICON_QUESTION=1, ICON_INFORMATION=1),
	}
	replaced = {name: sys.modules.get(name) for name in stubs}
	sys.modules.update(stubs)
	try:
		spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		sys.modules[MODULE_NAME] = module
		spec.loader.exec_module(module)
	finally:
		for name, previous in replaced.items():
			if previous is None:
				del sys.modules[name]
			else:
				sys.modules[name] = previous
	return module, stubs["globalVars"], stubs["addonHandler"], stubs["gui"]


install_tasks, globalVars, addonHandler, gui = _load_install_tasks()


@pytest.fixture
def addons(tmp_path, monkeypatch):
	"""Return an NVDA add-ons directory holding the add-on being installed."""
	directory = tmp_path / "addons"
	directory.mkdir()
	installation = directory / "TeleNVDA Accessolutions.pendingInstall"
	installation.mkdir()
	globalVars.appArgs.configPath = str(tmp_path)
	monkeypatch.setattr(addonHandler, "getAvailableAddons", lambda: [])
	monkeypatch.setattr(install_tasks, "_installation_directory", lambda: str(installation))
	return directory


def _make_addon_directory(parent, name):
	path = parent / name
	path.mkdir()
	(path / "manifest.ini").write_text("name = %s" % name, encoding="utf-8")
	return path


def test_legacy_addon_directories_are_deleted(addons):
	telenvda = _make_addon_directory(addons, "TeleNVDA")
	remote = _make_addon_directory(addons, "Remote")

	removed = install_tasks._remove_legacy_addons()

	assert sorted(removed) == ["Remote", "TeleNVDA"]
	assert not telenvda.exists()
	assert not remote.exists()


def test_the_addon_being_installed_and_the_others_are_kept(addons, monkeypatch):
	unrelated = _make_addon_directory(addons, "sayCurrentKeyboardLanguage")
	previous = _make_addon_directory(addons, "TeleNVDA Accessolutions")

	assert install_tasks._remove_legacy_addons() == []
	assert unrelated.exists()
	assert previous.exists()
	assert (addons / "TeleNVDA Accessolutions.pendingInstall").exists()


def test_installed_legacy_addons_are_removed_through_nvda(addons, monkeypatch):
	remote = Addon("remote", _make_addon_directory(addons, "remote"))
	monkeypatch.setattr(addonHandler, "getAvailableAddons", lambda: [remote])

	removed = install_tasks._remove_legacy_addons()

	assert removed == ["remote"]
	assert remote.remove_requested is True
	# NVDA completes the removal at its next start, the directory is still there.
	assert Path(remote.path).exists()


def test_a_loaded_legacy_addon_is_moved_out_of_the_way(addons, monkeypatch):
	telenvda = Addon("TeleNVDA", _make_addon_directory(addons, "TeleNVDA"), removable=False)
	monkeypatch.setattr(addonHandler, "getAvailableAddons", lambda: [telenvda])

	removed = install_tasks._remove_legacy_addons()

	assert removed == ["TeleNVDA"]
	assert not Path(telenvda.path).exists()


def test_nothing_is_removed_from_a_read_only_copy(addons, monkeypatch):
	telenvda = _make_addon_directory(addons, "TeleNVDA")
	monkeypatch.setattr(globalVars.appArgs, "secure", True)

	assert install_tasks._remove_legacy_addons() == []
	assert telenvda.exists()

	monkeypatch.setattr(globalVars.appArgs, "secure", False)


def test_the_daily_logs_survive_an_update(addons):
	previous = _make_addon_directory(addons, "TeleNVDA Accessolutions")
	(previous / "log").mkdir()
	(previous / "log" / "telenvda-2026-09-21.log").write_text("yesterday", encoding="utf-8")

	install_tasks._prepare_log_directory()

	kept = addons / "TeleNVDA Accessolutions.pendingInstall" / "log" / "telenvda-2026-09-21.log"
	assert kept.read_text(encoding="utf-8") == "yesterday"


def test_a_first_installation_gets_an_empty_log_directory(addons):
	install_tasks._prepare_log_directory()

	log_directory = addons / "TeleNVDA Accessolutions.pendingInstall" / "log"
	assert log_directory.is_dir()
	assert list(log_directory.iterdir()) == []
