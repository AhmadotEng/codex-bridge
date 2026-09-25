"""Optional Linux owner-session services; never system/root services or linger.

Only exact Bridge-owned units are changed. Registration and running processes are
independent: enable never starts and disable never stops. The wrapper supervises
the waiting daemon; persistent peer startup submits one bounded manager demand.
"""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

from .core import BridgeError, now


def _common():
    from . import autostart
    return autostart


def _require_linux():
    if not sys.platform.startswith('linux'):
        raise BridgeError('autostart_unavailable', 'Automatic owner-session startup requires Windows or Linux systemd. Use manual local startup on this platform.')
    if os.geteuid() == 0:
        raise BridgeError('owner_session_required', 'Run Bridge startup setup as the ordinary Codex owner, without sudo. Root services and lingering are not configured.')
    if not shutil.which('systemctl'):
        raise BridgeError('autostart_unavailable', 'systemctl is unavailable. Start Bridge manually with scripts/bridge.sh start --background.')


def _systemctl(*args, check=True):
    try:
        result = subprocess.run(['systemctl', '--user', *args], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeError('user_service_unavailable', 'Cannot reach the owner systemd user manager. Use a signed-in owner session or manual Bridge startup.') from exc
    if check and result.returncode:
        raise BridgeError('user_service_failed', 'The owner systemd operation failed. Inspect the selected Bridge user unit locally; existing work was not stopped.')
    return result


def _unit_directory():
    root = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config').expanduser()
    if not root.is_absolute():
        raise BridgeError('configuration', 'XDG_CONFIG_HOME must be an absolute owner configuration path.')
    return root / 'systemd' / 'user'


def unit_name(path, component, peer_id=None):
    _common()._validate(component, peer_id)
    scope = str(Path(path).expanduser().resolve()) + '\0' + component + '\0' + (peer_id or '')
    return 'codex-bridge-' + component + '-' + hashlib.sha256(scope.encode()).hexdigest()[:24] + '.service'


def _unit_value(value):
    value = str(value)
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise BridgeError('configuration', 'Service paths and arguments must be nonempty and contain no control characters.')
    return value


def _quote(value, *, command=False):
    value = _unit_value(value)
    # systemd syntax, not shell quoting. Escape its specifiers and ExecStart
    # environment substitution even inside quoted arguments.
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if command:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def _working_directory(value):
    # Unlike ExecStart/Environment, systemd's config_parse_working_directory
    # does not unquote or C-unescape: spaces, quotes, backslashes and $ are
    # literal here. Only percent specifiers need escaping. See systemd's
    # src/core/load-fragment.c and src/shared/conf-parser.c.
    value = _unit_value(value).replace('%', '%%')
    # A final slash preserves directory identity while preventing whitespace
    # stripping or line continuation for names ending in space or backslash.
    return value if value.endswith('/') else value + '/'


def unit_text(path, component, peer_id=None):
    _common()._validate(component, peer_id)
    if component == 'transport' and peer_id is None:
        raise BridgeError('persistent_peer_required', 'Persistent startup requires an explicit peer.')
    selected = Path(path).expanduser().resolve()
    source = Path(__file__).resolve().parents[1]
    scope = hashlib.sha256(str(selected).encode()).hexdigest()
    args = [str(Path(sys.executable).resolve()), '-m', 'codex_bridge.cli', '--config', str(selected),
            'autostart-run', '--component', component]
    if peer_id:
        args += ['--peer', peer_id]
    lines = ['# Codex Bridge owner unit ' + scope, '[Unit]',
             'Description=Codex Bridge ' + ('waiting daemon' if component == 'daemon' else 'explicit persistent peer demand'),
             '[Service]', 'Type=' + ('simple' if component == 'daemon' else 'oneshot'),
             'WorkingDirectory=' + _working_directory(source),
             'Environment=' + _quote('PYTHONPATH=' + str(source)),
             'ExecStart=' + ' '.join(_quote(arg, command=True) for arg in args),
             # No unit restart policy can regenerate connection budgets. The
             # daemon wrapper already has finite process restart handling.
             'Restart=no', 'UMask=0077', 'KillMode=control-group', 'TimeoutStopSec=25']
    if component == 'transport':
        lines += ['TimeoutStartSec=90']
    lines += ['', '[Install]', 'WantedBy=default.target', '']
    return '\n'.join(lines)


def _entry(path, component, peer_id=None):
    return {'enabled': True, 'component': component, 'peer_id': peer_id, 'backend': 'systemd-user',
            'unit_name': unit_name(path, component, peer_id),
            'unit_path': str(_unit_directory() / unit_name(path, component, peer_id)),
            'unit_sha256': hashlib.sha256(unit_text(path, component, peer_id).encode()).hexdigest()}


def _inspect(name):
    result = _systemctl('show', name, '--property=LoadState,ActiveState,UnitFileState,FragmentPath,DropInPaths', check=False)
    info = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode and info.get('LoadState') != 'not-found':
        raise BridgeError('user_service_unavailable', 'The owner user service manager is unavailable; use manual startup until an owner session is available.')
    if not info.get('LoadState'):
        raise BridgeError('user_service_unavailable', 'The owner user service manager did not return usable unit status; existing configuration was preserved.')
    return info


_VENDOR_TIMEOUT_POLICY = '/usr/lib/systemd/user/service.d/10-timeout-abort.conf'


def _reviewed_dropins(value):
    """Recognize the inspected Fedora type-wide stop-timeout policy only.

    A service.d drop-in is inherited by every service, but that alone does not
    make it safe: it can also replace commands or restart policy. Never trust a
    directory, filename, or root ownership without checking the contents too.
    Unknown policies and all owner/name-specific overrides remain conflicts.
    """
    if not value:
        return True
    # Compare the full property before parsing: additional paths, escaped paths,
    # aliases and per-unit copies must not acquire this exception.
    if value != _VENDOR_TIMEOUT_POLICY:
        return False
    selected = Path(value)
    try:
        for component in (*reversed(selected.parents), selected):
            info = component.lstat()
            expected = stat.S_ISREG if component == selected else stat.S_ISDIR
            if not expected(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022:
                return False
        # Parent directories and file are not replaceable by an ordinary owner.
        # Still check the opened inode and bound the read to reject replacement,
        # special files, and unexpectedly large policy files conservatively.
        with selected.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid, opened.st_gid) != (
                    info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid):
                return False
            raw = stream.read(16385)
        if len(raw) > 16384:
            return False
        text = raw.decode('utf-8')
        if any(ord(c) < 32 and c not in '\n\r\t' or ord(c) == 127 for c in text):
            return False
        lines = [line.strip(' \t\r') for line in text.split('\n')]
        # Do not implement systemd's continuation syntax in this narrow parser.
        if any(line.endswith('\\') for line in lines):
            return False
        settings = [line for line in lines if line and not line.startswith(('#', ';'))]
        return settings == ['[Service]', 'TimeoutStopFailureMode=abort']
    except (OSError, UnicodeError, ValueError):
        return False


def _check_owned(path, component, peer_id, data):
    common = _common()
    entry = _entry(path, component, peer_id)
    selected = Path(entry['unit_path'])
    old = common._entry_settings(data, component, peer_id)
    if selected.is_symlink():
        raise BridgeError('startup_ownership_conflict', 'The selected service file is a symlink; it was preserved.')
    raw = selected.read_bytes() if selected.exists() else None
    if raw is not None:
        digest = hashlib.sha256(raw).hexdigest()
        previous = old.get('unit_sha256') if old.get('unit_path') == str(selected) else None
        if digest not in (entry['unit_sha256'], previous):
            raise BridgeError('startup_ownership_conflict', 'The selected user unit differs from the saved Bridge registration; it was preserved.')
    info = _inspect(entry['unit_name'])
    fragment = info.get('FragmentPath')
    if (fragment and Path(fragment).resolve() != selected.resolve()) or not _reviewed_dropins(info.get('DropInPaths')):
        raise BridgeError('startup_ownership_conflict', 'Another user-unit path or override controls this name; inspect it before changing startup.')
    return entry, selected, raw, info


def _write(selected, raw):
    selected.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix='.bridge-unit-', dir=selected.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(raw)
        os.chmod(temporary, 0o600)
        os.replace(temporary, selected)
    finally:
        temporary.unlink(missing_ok=True)


def _restore(snapshots):
    # Restore the exact prior files and enabled state without ever starting or
    # stopping a process. Report any rollback failure instead of claiming repair.
    failed = False
    for entry, selected, raw, info in snapshots:
        try:
            if raw is None:
                selected.unlink(missing_ok=True)
            else:
                _write(selected, raw)
        except OSError:
            failed = True
    try:
        _systemctl('daemon-reload')
    except BridgeError:
        failed = True
    for entry, selected, raw, info in snapshots:
        try:
            _systemctl('enable' if info.get('UnitFileState') in ('enabled', 'enabled-runtime') else 'disable', entry['unit_name'])
        except BridgeError:
            failed = True
    if failed:
        raise BridgeError('startup_rollback_incomplete', 'Startup registration failed and its rollback was incomplete. Inspect the exact owner user units before retrying; no running work was stopped.')


def enable(path, component='auto', peer_id=None):
    _require_linux()
    common = _common(); cli = common._cli()
    selected = common._targets(path, component, peer_id, for_enable=True)
    data = common.settings(path)
    snapshots = [_check_owned(path, kind, peer, data) for kind, peer in selected]
    for (entry, unit, raw, info), (kind, peer) in zip(snapshots, selected):
        if info.get('ActiveState') in ('active', 'activating', 'deactivating', 'reloading') and raw != unit_text(path, kind, peer).encode():
            raise BridgeError('component_running', 'Stop the selected user unit before changing its launcher; running work was preserved.')
    updated = copy.deepcopy(data)
    try:
        for (entry, unit, raw, info), (kind, peer) in zip(snapshots, selected):
            _write(unit, unit_text(path, kind, peer).encode())
        _systemctl('daemon-reload')
        for entry, unit, raw, info in snapshots:
            _systemctl('enable', entry['unit_name'])
            mapping = updated['transports'] if entry['peer_id'] is not None else updated['components']
            mapping[entry['peer_id'] or entry['component']] = entry
        updated.update(version=2, updated_at=now())
        cli.save(common.state_path(path) / 'autostart.json', updated)
    except Exception:
        _restore(snapshots)
        raise
    return {'enabled_components': [{'component': kind, 'peer_id': peer} for kind, peer in selected],
            'started_now': False, 'units': [item[0]['unit_name'] for item in snapshots],
            'backend': 'systemd-user', 'linger_changed': False,
            'note': 'Enabled for the ordinary owner user session; no service was started. Daemon startup only waits. Persistent peer demand is separate and bounded.'}


def disable(path, component='all', remove=False, peer_id=None):
    _require_linux()
    common = _common(); cli = common._cli(); data = common.settings(path)
    selected = common._targets(path, component, peer_id)
    snapshots = [_check_owned(path, kind, peer, data) for kind, peer in selected]
    if remove:
        for (entry, unit, raw, info), (kind, peer) in zip(snapshots, selected):
            if info.get('ActiveState') in ('active', 'activating', 'deactivating', 'reloading') or cli.lock_held(common.runner_lock(path, kind, peer)) or cli.lock_held(common.component_lock(path, kind, peer)):
                raise BridgeError('component_running', 'Stop the selected component before removing its owner user unit.')
    updated = copy.deepcopy(data)
    try:
        for (entry, unit, raw, info), (kind, peer) in zip(snapshots, selected):
            if raw is not None:
                _systemctl('disable', entry['unit_name'])
            if remove:
                unit.unlink(missing_ok=True)
            mapping = updated['transports'] if peer is not None else updated['components']
            mapping.setdefault(peer or kind, entry).update(enabled=False, registration_removed=remove)
        if remove:
            _systemctl('daemon-reload')
        updated.update(version=2, updated_at=now())
        cli.save(common.state_path(path) / 'autostart.json', updated)
    except Exception:
        _restore(snapshots)
        raise
    return {'disabled_components': [{'component': kind, 'peer_id': peer} for kind, peer in selected],
            'registrations_removed': remove, 'running_work_stopped': False, 'backend': 'systemd-user'}


def start(path, component, peer_id=None):
    _require_linux()
    common = _common()
    if not common.enabled(path, component, peer_id):
        raise BridgeError('configuration', 'Owner-session startup is not enabled for the selected component.')
    entry, unit, raw, info = _check_owned(path, component, peer_id, common.settings(path))
    if raw is None or info.get('UnitFileState') not in ('enabled', 'enabled-runtime'):
        raise BridgeError('configuration', 'The selected owner unit is missing or disabled; run autostart-enable to repair registration.')
    _systemctl('start', entry['unit_name'])
    return {'launch_mode': 'owner-user-service', 'unit_name': entry['unit_name'], 'peer_id': peer_id, 'start_requested': True}


def status(path, peer_id=None):
    _require_linux()
    common = _common(); cli = common._cli(); data = common.settings(path)
    rows = []
    for kind, peer in common._targets(path, 'all', peer_id):
        row = {'component': kind, 'peer_id': peer, 'unit_name': unit_name(path, kind, peer),
               'configured_enabled': common.enabled(path, kind, peer),
               'stop_requested': common.stop_path(path, kind, peer).exists(),
               'runner': cli.record_status(common.runner_record(path, kind, peer), common.runner_lock(path, kind, peer))}
        try:
            entry, unit, raw, info = _check_owned(path, kind, peer, data)
            row.update(registered=raw is not None, owned=True, state=info.get('ActiveState'),
                       enabled=info.get('UnitFileState') in ('enabled', 'enabled-runtime'))
        except BridgeError as exc:
            row.update(owned=False, state='unknown', error=exc.as_dict())
        rows.append(row)
    return {'components': rows, 'backend': 'systemd-user', 'linger_changed': False,
            'readiness': 'User-unit/process state does not verify SSH, peer reachability, or Codex task completion. Reboot persistence is a separate observed test.'}
