import gui
import wx
import addonHandler
import buildVersion
import configobj
import globalVars
import os
import shutil
import tempfile
from config import conf as nvda_conf
from logHandler import log

addonHandler.initTranslation()

_NATIVE_REMOTE_SECTION = "native_remote"

#: Add-ons providing the very same remote control features as this one. Keeping them
#: side by side means two add-ons fighting over the same gestures and the same relay,
#: so they are removed when TeleNVDA Accessolutions is installed.
_LEGACY_ADDON_NAMES = frozenset(("telenvda", "remote"))
#: Directory holding the daily logs, carried over from the previous version.
_LOG_DIR_NAME = "log"
_PENDING_INSTALL_SUFFIX = ".pendingInstall"


def _as_bool(value):
	if isinstance(value, bool):
		return value
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	return bool(value)


def _is_readonly():
	return bool(
		getattr(globalVars.appArgs, "secure", False) or getattr(globalVars.appArgs, "launcher", False),
	)


def _load_addon_config():
	path = os.path.abspath(os.path.join(globalVars.appArgs.configPath, "teleNVDA.ini"))
	return configobj.ConfigObj(infile=path, default_encoding="utf8", create_empty=True)


def _read_native_remote_state():
	config = _load_addon_config()
	state = config.get(_NATIVE_REMOTE_SECTION)
	if state is None:
		return False, True
	return _as_bool(state.get("managed", False)), _as_bool(state.get("original_enabled", True))


def _save_native_remote_state(original_enabled):
	if _is_readonly():
		return False
	config = _load_addon_config()
	if _NATIVE_REMOTE_SECTION not in config:
		config[_NATIVE_REMOTE_SECTION] = {}
	state = config[_NATIVE_REMOTE_SECTION]
	state["managed"] = True
	state["original_enabled"] = bool(original_enabled)
	state["restore_on_reactivation"] = False
	config.write()
	return True


def _clear_native_remote_state():
	if _is_readonly():
		return False
	config = _load_addon_config()
	if _NATIVE_REMOTE_SECTION not in config:
		return True
	state = config[_NATIVE_REMOTE_SECTION]
	state["managed"] = False
	state["original_enabled"] = True
	state["restore_on_reactivation"] = False
	config.write()
	return True


def _manage_native_remote_on_install():
	if _is_readonly():
		return
	try:
		remote = nvda_conf.get("remote")
		if remote is None or "enabled" not in remote:
			return
		managed, original_enabled = _read_native_remote_state()
		if not managed:
			original_enabled = bool(remote["enabled"])
			if not _save_native_remote_state(original_enabled):
				return
		if remote["enabled"] is not False:
			remote["enabled"] = False
			nvda_conf.save()
	except Exception:
		log.exception("Unable to disable native NVDA Remote during TeleNVDA installation")


def _restore_native_remote_on_uninstall():
	if _is_readonly():
		return
	try:
		managed, original_enabled = _read_native_remote_state()
		if not managed:
			return
		remote = nvda_conf.get("remote")
		if remote is not None and "enabled" in remote:
			if remote["enabled"] != original_enabled:
				remote["enabled"] = original_enabled
				nvda_conf.save()
		_clear_native_remote_state()
	except Exception:
		log.exception("Unable to restore native NVDA Remote during TeleNVDA uninstallation")


def _installation_directory():
	"""Return the directory the add-on is being installed into."""
	return os.path.abspath(os.path.dirname(__file__))


def _addons_directory():
	return os.path.abspath(os.path.join(globalVars.appArgs.configPath, "addons"))


def _strip_pending_install(name):
	if name.casefold().endswith(_PENDING_INSTALL_SUFFIX.casefold()):
		return name[: -len(_PENDING_INSTALL_SUFFIX)]
	return name


def _is_legacy_addon_name(name):
	return _strip_pending_install(str(name).strip()).casefold() in _LEGACY_ADDON_NAMES


def _delete_addon_directory(path):
	"""Delete an add-on directory, even one NVDA currently has loaded.

	A loaded add-on keeps its Python modules open, so its files cannot be deleted
	before NVDA restarts. Its directory can still be moved out of the way though,
	which is what NVDA itself does: the leftovers are cleaned up at the next start.
	"""
	try:
		shutil.rmtree(path)
		return True
	except OSError:
		pass
	suffix = getattr(addonHandler, "DELETEDIR_SUFFIX", ".delete")
	try:
		pending_deletion = tempfile.mkdtemp(suffix=suffix, dir=os.path.dirname(path))
		os.rename(path, os.path.join(pending_deletion, "addon"))
	except OSError:
		log.exception("Unable to remove the old add-on directory %s" % path)
		return False
	shutil.rmtree(pending_deletion, ignore_errors=True)
	return True


def _remove_legacy_addons():
	"""Remove the previous remote control add-ons, and return the names removed."""
	if _is_readonly():
		return []
	installation_directory = os.path.normcase(_installation_directory())
	addons_directory = _addons_directory()
	user_addons = os.path.normcase(addons_directory) + os.sep
	removed = []
	visited = set()
	try:
		available = list(addonHandler.getAvailableAddons())
	except Exception:
		log.exception("Unable to list the installed add-ons")
		available = []
	for addon in available:
		path = os.path.abspath(getattr(addon, "path", "") or "")
		if os.path.normcase(path) == installation_directory:
			continue
		# Only the add-ons of this NVDA profile are ours to remove: anything NVDA
		# ships with itself or runs from the scratchpad is left alone.
		if not os.path.normcase(path).startswith(user_addons):
			continue
		if not _is_legacy_addon_name(addon.name) and not _is_legacy_addon_name(os.path.basename(path)):
			continue
		visited.add(os.path.normcase(path))
		try:
			addon.requestRemove()
		except Exception:
			log.exception("Unable to remove the %s add-on" % addon.name)
			if not (path and os.path.isdir(path) and _delete_addon_directory(path)):
				continue
		removed.append(addon.name)
	# Directories NVDA no longer lists as add-ons, typically left behind by a removal
	# which could not be completed or by a manifest it failed to read.
	try:
		entries = os.listdir(addons_directory)
	except OSError:
		entries = []
	for entry in entries:
		path = os.path.join(addons_directory, entry)
		if os.path.normcase(path) in visited or not os.path.isdir(path):
			continue
		if not _is_legacy_addon_name(entry):
			continue
		if _delete_addon_directory(path):
			removed.append(_strip_pending_install(entry))
	return removed


def _report_legacy_addons_removed(names):
	if _is_readonly() or getattr(globalVars.appArgs, "minimal", False):
		return
	gui.messageBox(
		# Translators: message shown when old remote control add-ons were removed
		_(
			"The following add-ons offer the same remote assistance as TeleNVDA and have been removed to avoid conflicts: {addons}.\nTheir removal will be complete once NVDA has restarted.",
		).format(addons=", ".join(sorted(names))),
		# Translators: title of the message reporting the removal of old add-ons
		_("Old remote control add-ons removed"),
		wx.OK | wx.ICON_INFORMATION,
		gui.mainFrame,
	)


def _prepare_log_directory():
	"""Create the directory of the daily logs, carrying the previous ones over.

	NVDA replaces the whole add-on directory when it completes an update, which would
	otherwise throw away the history kept next to the add-on.
	"""
	destination = os.path.join(_installation_directory(), _LOG_DIR_NAME)
	if os.path.isdir(destination):
		return
	previous = os.path.join(
		os.path.dirname(_installation_directory()),
		_strip_pending_install(os.path.basename(_installation_directory())),
	)
	source = os.path.join(previous, _LOG_DIR_NAME)
	try:
		if os.path.normcase(source) != os.path.normcase(destination) and os.path.isdir(source):
			shutil.copytree(source, destination)
		else:
			os.makedirs(destination, exist_ok=True)
	except OSError:
		log.exception("Unable to prepare the TeleNVDA log directory")


def onInstall():
	removed = _remove_legacy_addons()
	if removed:
		_report_legacy_addons_removed(removed)
	_prepare_log_directory()
	_manage_native_remote_on_install()
	if buildVersion.version_year >= 2026 or any(name.casefold() == "remote" for name in removed):
		# NVDA Remote is on its way out, asking to disable it would only confuse.
		return
	for addon in addonHandler.getAvailableAddons():
		if addon.name == "remote" and not addon.isDisabled:
			result = gui.messageBox(
				# Translators: message asking the user wether NVDA Remote whould be disabled or not
				_(
					"NVDA Remote has been detected on your NVDA installation. In order for TeleNVDA to work without conflicts, NVDA Remote must be disabled. Otherwise, TeleNVDA will refuse to work. Would you like to disable NVDA Remote now?",
				),
				# Translators: question title
				_("Running NVDA Remote detected"),
				wx.YES_NO | wx.ICON_QUESTION,
				gui.mainFrame,
			)
			if result == wx.YES:
				addon.enable(False)
			return


def onUninstall():
	_restore_native_remote_on_uninstall()
