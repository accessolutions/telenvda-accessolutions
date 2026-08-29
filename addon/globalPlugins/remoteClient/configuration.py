from io import StringIO
import os
import tempfile
import time
import configobj
from configobj import validate
import globalVars
from . import socket_utils
readonly = globalVars.appArgs.secure or globalVars.appArgs.launcher

CONFIG_FILE_NAME = 'teleNVDA.ini'
DEFAULT_SERVER_HOST = "nvdaremote.accessolutions.fr"
LEGACY_SERVER_HOSTS = frozenset(("remote.nvda.es",))
LEGACY_CONFIG_FILE_NAME = 'remote.ini'

# Default relay servers offered in every server list, in addition to any address
# the user has already connected to. The Accessolutions relay is intentionally
# not listed here: it remains the default host for new auto-connect settings,
# but can still be entered manually when starting a connection.
DEFAULT_SERVER_HOSTS = ("nvda.fr", "nvdaremote.com")

# Addresses which may already exist in the connection history but should no
# longer be suggested by the connection dialog.
HIDDEN_SERVER_ADDRESSES = frozenset(("nvdaremote.accessolutions.fr:443",))

# Default number of seconds of inactivity (no real remote control action
# performed or received) after which auto-connect is automatically turned off.
DEFAULT_INACTIVITY_AUTO_DISABLE_SECONDS = 60 * 60 * 24 * 30

# Minimum delay, in seconds, between two writes of the activity timestamp to
# disk. Real activity (e.g. key presses) can happen very frequently and we
# don't want to hit the disk on every single one of them.
_MIN_ACTIVITY_WRITE_INTERVAL = 5
_last_activity_write_time = 0.0

_config = None
configspec = StringIO("""
[connections]
	last_connected = list(default=list("nvdaremote.accessolutions.fr"))
[controlserver]
	autoconnect = boolean(default=False)
	self_hosted = boolean(default=False)
	UPNP = boolean(default=False)
	connection_type = integer(default=0)
	host = string(default="nvdaremote.accessolutions.fr")
	port = integer(default=6837)
	key = string(default="")
	encryption_key = string(default="")
	transport = option("tcp", "websocket", default="tcp")
	ws_path = string(default="/")
	proxy_mode = option("manual", "auto", "none", default="auto")
	proxy_host = string(default="")
	proxy_port = integer(default=0)
	proxy_username = string(default="")
	proxy_password = string(default="")
	proxy_type = option("http", "socks4", "socks4a", "socks5", "socks5h", "negotiate", "ntlm", default="http")
	disable_autoconnect_after_inactivity = boolean(default=True)
	inactivity_auto_disable_seconds = integer(default=2592000)

[seen_motds]
	__many__ = string(default="")

[trusted_certs]
	__many__ = string(default="")

[activity]
	last_activity_timestamp = float(default=0.0)

[native_remote]
	managed = boolean(default=False)
	original_enabled = boolean(default=True)
	restore_on_reactivation = boolean(default=False)
	settings_imported = boolean(default=False)

[updates]
	check_at_startup = boolean(default=True)

[screenshots]
	directory = string(default="")

[file_transfer]
	allow_large_legacy_transfers = boolean(default=False)
	legacy_max_size_mb = integer(default=100)
	max_received_size_mb = integer(default=0)

[screen_share]
	enabled = boolean(default=True)
	max_fps = integer(default=15)
	max_width = integer(default=1600)
	quality = option("low", "balanced", "high", default="balanced")
	share_audio = boolean(default=True)
	mute_remote_speech_with_audio = boolean(default=True)

[keep_awake]
	enabled = boolean(default=True)
	delay_seconds = integer(default=60)
	max_duration_minutes = integer(default=0)

[ui]
	play_sounds = boolean(default=True)
	alert_before_slave_disconnect = boolean(default=True)
	mute_when_controlling_local_machine = boolean(default=False)
	allow_speech_commands = boolean(default=True)
	display_motd_once = boolean(default=False)
	portcheck = string(default="https://nvda.es/portcheck.php?port={port}")
""")

def is_hidden_server_address(address):
	"""Return whether an address should be omitted from the connection history list."""
	return str(address).strip().casefold() in HIDDEN_SERVER_ADDRESSES

def normalize_server_host(host):
	"""Return the current relay hostname without an embedded port."""
	original = str(host or '').strip()
	if not original:
		return original
	try:
		parsed_host, _ = socket_utils.address_to_hostport(original, default_port=0)
	except (TypeError, ValueError):
		return original
	if not parsed_host:
		return original
	if parsed_host.casefold() in LEGACY_SERVER_HOSTS:
		return DEFAULT_SERVER_HOST
	return parsed_host

def _migrate_server_configuration(config):
	"""Migrate old relay host names and embedded TCP ports."""
	changed = False
	section = config['controlserver']
	if not section.get('self_hosted', False):
		raw_host = str(section.get('host', '') or '').strip()
		if raw_host:
			try:
				host, port = socket_utils.address_to_hostport(
					raw_host,
					default_port=int(section.get('port', socket_utils.SERVER_PORT) or socket_utils.SERVER_PORT),
				)
			except (TypeError, ValueError):
				host, port = raw_host, None
			canonical_host = normalize_server_host(host)
			if canonical_host != raw_host:
				section['host'] = canonical_host
				changed = True
			if port is not None:
				# Old native/TeleNVDA TCP settings could contain :443 in the
				# host. The native Remote protocol uses the standard TCP port;
				# WebSocket connections must keep 443 explicitly.
				transport = str(section.get('transport', 'tcp')).lower()
				is_known_relay = canonical_host.casefold() == DEFAULT_SERVER_HOST.casefold()
				should_use_tcp_default = (
					port == 443
					and transport != 'websocket'
					and is_known_relay
				)
				new_port = socket_utils.SERVER_PORT if should_use_tcp_default else port
				if section.get('port') != new_port:
					section['port'] = new_port
					changed = True

	connections = config['connections']
	last_connected = connections.get('last_connected', [])
	canonical_history = []
	for address in last_connected:
		raw_address = str(address).strip()
		try:
			host, port = socket_utils.address_to_hostport(raw_address)
		except (TypeError, ValueError):
			canonical_history.append(address)
			continue
		if not host:
			canonical_history.append(address)
			continue
		canonical_address = socket_utils.hostport_to_address((normalize_server_host(host), port))
		canonical_history.append(canonical_address)
		if canonical_address != address:
			changed = True
	if canonical_history != list(last_connected):
		connections['last_connected'] = canonical_history
		changed = True
	return changed

def _has_configured_remote_settings(config):
	"""Return whether the active TeleNVDA profile already contains user data."""
	if has_explicit_remote_settings(config):
		return True
	return list(config['connections'].get('last_connected', [])) != [DEFAULT_SERVER_HOST]

def has_explicit_remote_settings(config=None):
	if config is None:
		config = get_config()
	section = config['controlserver']
	if section.get('autoconnect') or section.get('self_hosted') or section.get('key'):
		return True
	if section.get('UPNP') or section.get('encryption_key') or section.get('connection_type'):
		return True
	if normalize_server_host(section.get('host')) != DEFAULT_SERVER_HOST:
		return True
	try:
		if int(section.get('port', socket_utils.SERVER_PORT)) != socket_utils.SERVER_PORT:
			return True
	except (TypeError, ValueError):
		return True
	if (
		section.get('transport', 'tcp') != 'tcp'
		or section.get('ws_path', '/') != '/'
		or section.get('proxy_mode', 'auto') != 'auto'
		or section.get('proxy_host')
		or section.get('proxy_port')
		or section.get('proxy_username')
		or section.get('proxy_password')
	):
		return True
	return False

def _get_control_server_section(config):
	return (
		config.get('controlserver')
		or config.get('controlServer')
		or config.get('control_server')
	)

def _coerce_config_value(value, current_value):
	if isinstance(current_value, bool):
		if isinstance(value, str):
			return value.strip().casefold() in ('1', 'true', 'yes', 'on')
		return bool(value)
	if isinstance(current_value, int):
		try:
			return int(value)
		except (TypeError, ValueError):
			return current_value
	return value

def migrate_external_addon_settings():
	"""Import an old TeleNVDA/NVDA Remote profile into a pristine profile.

	This is intentionally limited to portable NVDA instances. A portable copy
	must not silently replace settings which were already configured locally.
	"""
	if readonly or not _is_portable_copy():
		return False
	config = get_config()
	if _has_configured_remote_settings(config):
		return False
	for path in get_legacy_addon_config_paths():
		source = load_external_config(path)
		if source is None:
			continue
		section = _get_control_server_section(source)
		if not section:
			continue
		current = config['controlserver']
		key_aliases = {
			'autoconnect': ('autoconnect',),
			'self_hosted': ('self_hosted', 'selfHosted'),
			'UPNP': ('UPNP', 'upnp'),
			'connection_type': ('connection_type', 'connectionMode'),
			'host': ('host',),
			'port': ('port',),
			'key': ('key',),
			'encryption_key': ('encryption_key', 'encryptionKey'),
			'transport': ('transport',),
			'ws_path': ('ws_path', 'wsPath'),
			'proxy_mode': ('proxy_mode', 'proxyMode'),
			'proxy_host': ('proxy_host', 'proxyHost'),
			'proxy_port': ('proxy_port', 'proxyPort'),
			'proxy_username': ('proxy_username', 'proxyUsername'),
			'proxy_password': ('proxy_password', 'proxyPassword'),
			'proxy_type': ('proxy_type', 'proxyType'),
			'disable_autoconnect_after_inactivity': ('disable_autoconnect_after_inactivity',),
			'inactivity_auto_disable_seconds': ('inactivity_auto_disable_seconds',),
		}
		for key, aliases in key_aliases.items():
			for alias in aliases:
				if alias in section and key in current:
					current[key] = _coerce_config_value(section[alias], current[key])
					break
		connections = source.get('connections')
		if connections and connections.get('last_connected'):
			config['connections']['last_connected'] = list(connections['last_connected'])
		for section_name in ('ui', 'updates', 'screenshots', 'file_transfer', 'screen_share', 'keep_awake'):
			source_section = source.get(section_name)
			target_section = config.get(section_name)
			if not source_section or not target_section:
				continue
			for key in target_section:
				if key in source_section:
					target_section[key] = _coerce_config_value(source_section[key], target_section[key])
		for section_name in ('seen_motds', 'trusted_certs'):
			source_section = source.get(section_name)
			if not source_section:
				continue
			for address, value in source_section.items():
				try:
					host, port = socket_utils.address_to_hostport(address)
				except (TypeError, ValueError):
					canonical_address = address
				else:
					canonical_address = socket_utils.hostport_to_address(
						(normalize_server_host(host), port),
					)
				config[section_name][canonical_address] = value
		_migrate_server_configuration(config)
		if config['controlserver'].get('autoconnect'):
			config['activity']['last_activity_timestamp'] = 0.0
		config.write()
		return True
	return False

def _is_portable_copy():
	try:
		import config as nvda_config
		return not nvda_config.isInstalledCopy()
	except (AttributeError, ImportError, OSError):
		return True

def get_portable_migration_config_dirs():
	"""Return the installed user-config directory when NVDA is portable."""
	if not _is_portable_copy():
		return []
	try:
		import config as nvda_config
		get_installed_path = getattr(nvda_config, 'getInstalledUserConfigPath', None)
		if get_installed_path is not None:
			installed_dir = get_installed_path()
		else:
			installed_dir = nvda_config.getUserDefaultConfigPath(useInstalledPathIfExists=True)
	except (AttributeError, ImportError, OSError, TypeError):
		return []
	if not installed_dir:
		return []
	current_dir = os.path.abspath(globalVars.appArgs.configPath)
	installed_dir = os.path.abspath(installed_dir)
	if os.path.normcase(current_dir) == os.path.normcase(installed_dir):
		return []
	return [installed_dir] if os.path.isdir(installed_dir) else []

def load_external_config(path):
	"""Load an optional ConfigObj file used for profile migration."""
	try:
		if not os.path.isfile(path):
			return None
		return configobj.ConfigObj(infile=path, default_encoding='utf8')
	except (OSError, configobj.ConfigObjError, UnicodeError):
		return None

def get_portable_native_config_paths():
	return [os.path.join(directory, 'nvda.ini') for directory in get_portable_migration_config_dirs()]

def get_legacy_addon_config_paths():
	"""Return TeleNVDA and old NVDA Remote config paths to inspect."""
	current_dir = os.path.abspath(globalVars.appArgs.configPath)
	paths = [os.path.join(current_dir, LEGACY_CONFIG_FILE_NAME)]
	for directory in get_portable_migration_config_dirs():
		paths.extend(
			(
				os.path.join(directory, CONFIG_FILE_NAME),
				os.path.join(directory, LEGACY_CONFIG_FILE_NAME),
			)
		)
	return paths

def _migrate_proxy_mode(config):
	"""Switch configurations left in manual mode without a proxy host to automatic detection.

	Manual mode with an empty host means no proxy at all, which silently breaks WebSocket
	connections behind a corporate proxy. Automatic Windows detection falls back to the same
	behaviour when no proxy is configured on the system, so the migration is safe.
	"""
	section = config['controlserver']
	if section.get('proxy_mode') == 'manual' and not section.get('proxy_host', '').strip():
		section['proxy_mode'] = 'auto'
		return True
	return False

def get_config():
	global _config
	if not _config:
		path = os.path.abspath(os.path.join(globalVars.appArgs.configPath, CONFIG_FILE_NAME))
		_config = configobj.ConfigObj(infile=path, configspec=configspec, default_encoding='utf8', create_empty=not readonly)
		val = validate.Validator()
		_config.validate(val, copy=True)
		migrated = _migrate_proxy_mode(_config)
		migrated = _migrate_server_configuration(_config) or migrated
		if migrated and not readonly:
			try:
				_config.write()
			except Exception:
				pass
	return _config

def get_screenshot_directory():
	"""Return the configured screenshot directory or the current user's temp directory."""
	configured = get_config()['screenshots'].get('directory', '').strip()
	if configured:
		configured = os.path.abspath(os.path.expanduser(os.path.expandvars(configured)))
		if os.path.isdir(configured) and os.access(configured, os.W_OK):
			return configured
	return tempfile.gettempdir()

def get_native_remote_state():
	"""Return whether TeleNVDA manages native NVDA Remote and its original state."""
	state = get_config()['native_remote']
	return state['managed'], state['original_enabled']

def save_native_remote_state(original_enabled):
	"""Remember the native NVDA Remote state before TeleNVDA disables it."""
	if readonly:
		return False
	state = get_config()['native_remote']
	state['managed'] = True
	state['original_enabled'] = bool(original_enabled)
	state['restore_on_reactivation'] = False
	get_config().write()
	return True

def should_restore_native_remote_on_reactivation():
	"""Return whether native NVDA Remote must be restored after re-enabling TeleNVDA."""
	return get_config()['native_remote'].get('restore_on_reactivation', False)

def mark_native_remote_for_reactivation():
	"""Remember that TeleNVDA is being disabled before the next NVDA restart."""
	if readonly:
		return False
	state = get_config()['native_remote']
	if not state['managed']:
		return False
	state['restore_on_reactivation'] = True
	get_config().write()
	return True

def clear_native_remote_state():
	"""Forget the native NVDA Remote state after restoring it."""
	if readonly:
		return False
	state = get_config()['native_remote']
	state['managed'] = False
	state['original_enabled'] = True
	state['restore_on_reactivation'] = False
	get_config().write()
	return True

def were_native_remote_settings_imported():
	"""Return whether the automatic connection of native NVDA Remote was already looked at."""
	return get_config()['native_remote'].get('settings_imported', False)

def mark_native_remote_settings_imported():
	"""Remember that a valid native NVDA Remote connection was imported or kept.

	The marker prevents a later startup from replacing a user's deliberate TeleNVDA
	changes, while a startup without native settings deliberately leaves it unset.
	"""
	if readonly:
		return False
	get_config()['native_remote']['settings_imported'] = True
	get_config().write()
	return True

def trust_certificate(address, fingerprint):
	"""Trust a server certificate when its fingerprint was obtained successfully."""
	if not fingerprint:
		return False
	config = get_config()
	config['trusted_certs'][socket_utils.hostport_to_address(address)] = fingerprint
	if not readonly:
		config.write()
	return True

def write_connection_to_config(address, transport_type='tcp'):
	"""Writes an address to the last connected section of the config.
	If the address is already in the config, move it to the end."""
	conf = get_config()
	last_cons = conf['connections']['last_connected']
	host, port = address
	host = normalize_server_host(host)
	if (
		transport_type != 'websocket'
		and port == 443
		and host.casefold() == DEFAULT_SERVER_HOST.casefold()
	):
		port = socket_utils.SERVER_PORT
	address = socket_utils.hostport_to_address((host, port))
	if address in last_cons:
		conf['connections']['last_connected'].remove(address)
	conf['connections']['last_connected'].append(address)
	if not readonly:
		conf.write()

def record_activity():
	"""Record that a real remote control action was just performed or received
	(e.g. a key press, clipboard push, file transfer, braille input or SAS).
	This is used to automatically disable auto-connect on startup once no such
	activity has occurred for a long time (see should_disable_autoconnect_for_inactivity)."""
	global _last_activity_write_time
	if readonly:
		return
	conf = get_config()
	now = time.time()
	conf['activity']['last_activity_timestamp'] = now
	if now - _last_activity_write_time >= _MIN_ACTIVITY_WRITE_INTERVAL:
		conf.write()
		_last_activity_write_time = now

def flush_activity():
	"""Force any pending (throttled) activity timestamp to be written to disk.
	Should be called when the add-on terminates so recent activity is not lost."""
	if readonly:
		return
	get_config().write()

def parse_inactivity_duration(value):
	"""Convert a jj:hh:mm inactivity duration to seconds."""
	parts = value.strip().split(":")
	if len(parts) != 3 or not parts[0].isdigit() or any(
		len(part) != 2 or not part.isdigit() for part in parts[1:]
	):
		raise ValueError
	days, hours, minutes = (int(part) for part in parts)
	if hours > 23 or minutes > 59:
		raise ValueError
	seconds = days * 24 * 60 * 60 + hours * 60 * 60 + minutes * 60
	if seconds <= 0:
		raise ValueError
	return seconds

def format_inactivity_duration(seconds):
	"""Convert an inactivity duration in seconds to jj:hh:mm."""
	minutes, _ = divmod(int(seconds), 60)
	days, minutes = divmod(minutes, 24 * 60)
	hours, minutes = divmod(minutes, 60)
	return "{:02d}:{:02d}:{:02d}".format(days, hours, minutes)

def get_inactivity_auto_disable_seconds():
	"""Return the configured inactivity duration, falling back to the default."""
	seconds = get_config()['controlserver'].get(
		'inactivity_auto_disable_seconds',
		DEFAULT_INACTIVITY_AUTO_DISABLE_SECONDS,
	)
	try:
		seconds = int(seconds)
	except (TypeError, ValueError):
		return DEFAULT_INACTIVITY_AUTO_DISABLE_SECONDS
	if seconds <= 0 or seconds % 60:
		return DEFAULT_INACTIVITY_AUTO_DISABLE_SECONDS
	return seconds

def get_inactivity_timeout_remaining():
	"""Return seconds remaining before auto-connect must be disabled, or None."""
	conf = get_config()
	cs = conf['controlserver']
	if not cs['autoconnect'] or not cs['disable_autoconnect_after_inactivity']:
		return None
	last_activity = conf['activity']['last_activity_timestamp']
	if not last_activity:
		return None
	return max(0.0, last_activity + get_inactivity_auto_disable_seconds() - time.time())

def should_disable_autoconnect_for_inactivity():
	"""Return whether auto-connect should now be disabled because no real
	remote control activity has been recorded for more than
	the configured inactivity duration. A machine that has never recorded any
	activity is not considered inactive, to avoid disabling a freshly
	configured auto-connect before it was ever used."""
	remaining = get_inactivity_timeout_remaining()
	return remaining is not None and remaining <= 0
