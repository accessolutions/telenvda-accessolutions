from io import StringIO
import os
import time
import configobj
from configobj import validate
import globalVars
from . import socket_utils
readonly = globalVars.appArgs.secure or globalVars.appArgs.launcher

CONFIG_FILE_NAME = 'teleNVDA.ini'

# Default relay servers offered in every server list, in addition to any address
# the user has already connected to. nvdaremote.accessolutions.fr is offered
# first unless the user has already used another connection, followed by
# nvda.fr and nvdaremote.com (TCP).
DEFAULT_SERVER_HOSTS = ("nvdaremote.accessolutions.fr", "nvda.fr", "nvdaremote.com")

# Number of seconds of inactivity (no real remote control action performed or
# received) after which auto-connect on startup is automatically turned off.
# TODO(release): this is temporarily set to 1 minute for testing purposes.
# Restore to 30 days (60 * 60 * 24 * 30) before shipping.
INACTIVITY_AUTO_DISABLE_SECONDS = 60

# Minimum delay, in seconds, between two writes of the activity timestamp to
# disk. Real activity (e.g. key presses) can happen very frequently and we
# don't want to hit the disk on every single one of them.
_MIN_ACTIVITY_WRITE_INTERVAL = 5
_last_activity_write_time = 0.0

_config = None
configspec = StringIO("""
[connections]
	last_connected = list(default=list("remote.nvda.es"))
[controlserver]
	autoconnect = boolean(default=False)
	self_hosted = boolean(default=False)
	UPNP = boolean(default=False)
	connection_type = integer(default=0)
	host = string(default="remote.nvda.es")
	port = integer(default=6837)
	key = string(default="")
	encryption_key = string(default="")
	transport = option("tcp", "websocket", default="tcp")
	ws_path = string(default="/")
	proxy_host = string(default="")
	proxy_port = integer(default=0)
	proxy_username = string(default="")
	proxy_password = string(default="")
	proxy_type = option("http", "socks4", "socks4a", "socks5", "socks5h", "negotiate", "ntlm", default="http")
	disable_autoconnect_after_inactivity = boolean(default=True)

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

[updates]
	check_at_startup = boolean(default=True)
	channel = option("stable", "dev", default="stable")

[ui]
	play_sounds = boolean(default=True)
	alert_before_slave_disconnect = boolean(default=True)
	mute_when_controlling_local_machine = boolean(default=False)
	allow_speech_commands = boolean(default=True)
	display_motd_once = boolean(default=False)
	portcheck = string(default="https://nvda.es/portcheck.php?port={port}")
""")
def get_config():
	global _config
	if not _config:
		path = os.path.abspath(os.path.join(globalVars.appArgs.configPath, CONFIG_FILE_NAME))
		_config = configobj.ConfigObj(infile=path, configspec=configspec, default_encoding='utf8', create_empty=not readonly)
		val = validate.Validator()
		_config.validate(val, copy=True)
	return _config

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

def trust_certificate(address, fingerprint):
	"""Trust a server certificate when its fingerprint was obtained successfully."""
	if not fingerprint:
		return False
	config = get_config()
	config['trusted_certs'][socket_utils.hostport_to_address(address)] = fingerprint
	if not readonly:
		config.write()
	return True

def write_connection_to_config(address):
	"""Writes an address to the last connected section of the config.
	If the address is already in the config, move it to the end."""
	conf = get_config()
	last_cons = conf['connections']['last_connected']
	address = socket_utils.hostport_to_address(address)
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

def should_disable_autoconnect_for_inactivity():
	"""Return whether auto-connect should now be disabled because no real
	remote control activity has been recorded for more than
	INACTIVITY_AUTO_DISABLE_SECONDS. A machine that has never recorded any
	activity is not considered inactive, to avoid disabling a freshly
	configured auto-connect before it was ever used."""
	conf = get_config()
	cs = conf['controlserver']
	if not cs['autoconnect'] or not cs['disable_autoconnect_after_inactivity']:
		return False
	last_activity = conf['activity']['last_activity_timestamp']
	if not last_activity:
		return False
	return (time.time() - last_activity) > INACTIVITY_AUTO_DISABLE_SECONDS
