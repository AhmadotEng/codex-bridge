"""Local setup helpers. No credential contents or raw subprocess output is returned.

Only explicitly selected Bridge files are written. Codex authentication and SSH
identities stay where their owner configured them. Callers can inject prompts and
runtime probes for unattended setup and disposable-workspace tests.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request

from .core import BridgeError, canonical, identifier, now, safe_path


def _admin():
    from . import cli
    return cli


def _run(argv, timeout=30):
    options = {'capture_output': True, 'text': True, 'timeout': timeout,
               'stdin': subprocess.DEVNULL}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    return subprocess.run([str(x) for x in argv], **options)


def ask(label, default=None, *, batch=False, prompt=input, required=True):
    if batch:
        value = default
    else:
        suffix = f' [{default}]' if default is not None else ''
        value = prompt(label + suffix + ': ').strip() or default
    if required and (value is None or value == ''):
        raise BridgeError('missing_value', 'A required setup value was not supplied: ' + label)
    return value


def executable_path(value):
    if not value:
        return None
    selected = shutil.which(str(value)) or str(Path(value).expanduser())
    selected = Path(selected)
    return str(selected.resolve()) if selected.is_file() else None


def detect_codex(*, environ=None):
    env = os.environ if environ is None else environ
    # Explicit environment/PATH selections win. Otherwise prefer the newest
    # desktop runtime. This does not read account or browser storage.
    for candidate in (env.get('CODEX_CLI_PATH'), shutil.which('codex')):
        if candidate and executable_path(candidate):
            return executable_path(candidate)
    local = env.get('LOCALAPPDATA')
    candidates = []
    if local:
        base = Path(local) / 'OpenAI' / 'Codex' / 'bin'
        if base.is_dir():
            candidates = list(base.glob('*/codex.exe')) + list(base.glob('codex.exe'))
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(candidates[0].resolve()) if candidates else None


def inspect_runtime(codex_path):
    from .compatibility import inspect_runtime as inspect
    return inspect(codex_path)


def auth_status(codex_path, *, runner=_run):
    """Use the supported CLI status operation; never open an authentication file."""
    try:
        result = runner([codex_path, 'login', 'status'], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return {'state': 'unknown', 'code': 'auth_check_failed',
                'message': 'The local Codex sign-in check could not run.'}
    if result.returncode == 0:
        return {'state': 'pass', 'code': 'signed_in',
                'message': 'The selected local Codex runtime reports a saved sign-in.'}
    return {'state': 'fail', 'code': 'sign_in_required',
            'message': 'Sign into Codex locally on this computer, then run preflight again.'}


def register_mcp(codex_path, config_path, *, runner=_run):
    """Register one named entry, refusing to replace an unrelated registration."""
    source = Path(__file__).resolve().parent
    expected = {'command': sys.executable,
                'args': ['-u', str(source / 'mcp.py'), '--config', str(Path(config_path).resolve())]}
    try:
        listing = runner([codex_path, 'mcp', 'list', '--json'], timeout=30)
        if listing.returncode:
            raise BridgeError('registration_check_failed', 'Codex could not list its MCP registrations; no entry was changed.')
        entries = json.loads(listing.stdout)
        if not isinstance(entries, list):
            raise ValueError('invalid list')
        current = next((item for item in entries if item.get('name') == 'codex_bridge'), None)
        if current:
            transport = current.get('transport', {})
            command = executable_path(transport.get('command'))
            if (command == executable_path(expected['command']) and
                    transport.get('args') == expected['args'] and current.get('enabled', True)):
                return {'state': 'pass', 'already_registered': True, 'name': 'codex_bridge'}
            raise BridgeError('registration_conflict', 'A different codex_bridge MCP entry already exists. It was preserved; inspect it locally before replacing it.')
        result = runner([codex_path, 'mcp', 'add', 'codex_bridge', '--',
                         expected['command'], *expected['args']], timeout=30)
        if result.returncode:
            raise BridgeError('registration_failed', 'Codex could not register the connector. Existing settings were preserved.')
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        raise BridgeError('registration_check_failed', 'The local MCP registration check failed; inspect Codex locally.') from exc
    return {'state': 'pass', 'registered': True, 'name': 'codex_bridge',
            'next_step': 'Start a fresh local Codex chat to load the connector.'}


def setup(path, *, peer_id=None, codex=None, port=None, batch=False, prompt=input,
          runtime_probe=inspect_runtime, register=False, runner=_run):
    admin = _admin()
    path = Path(path).expanduser().resolve()
    existing = path.exists()
    cfg = admin.read_config(path) if existing else {}
    chosen_id = peer_id or cfg.get('peer_id')
    if not chosen_id:
        chosen_id = ask('A short name for this computer', batch=batch, prompt=prompt)
    identifier(chosen_id, 'peer_id')
    if existing and chosen_id != cfg['peer_id']:
        raise BridgeError('identity_change_denied', 'Existing computer identity was preserved. Use a separate configuration directory for a different computer.')
    selected_codex = codex or cfg.get('codex_path') or detect_codex()
    if not executable_path(selected_codex):
        selected_codex = ask('Full path to the local Codex executable', batch=batch, prompt=prompt)
    selected_codex = executable_path(selected_codex)
    if not selected_codex:
        raise BridgeError('runtime_missing', 'The selected local Codex executable does not exist.')
    selected_port = port if port is not None else cfg.get('listen_port', 47321)
    admin.port_number(selected_port, 'listen_port')
    if selected_port < 1024:
        raise BridgeError('configuration', 'Select a local bridge port between 1024 and 65535.')
    if sys.version_info < (3, 11):
        raise BridgeError('python_unsupported', 'Codex Bridge requires Python 3.11 or newer.')
    runtime = runtime_probe(selected_codex)
    if not runtime.get('ready'):
        raise BridgeError('runtime_incompatible', 'The selected Codex App Server schema is not compatible. Run preflight for a safe compatibility report.')
    if not existing:
        state = path.parent / 'state'
        if state.exists() and any(state.iterdir()):
            raise BridgeError('already_initialized', 'Existing state was preserved; select a fresh configuration directory or restore its matching configuration.')
        if not path.parent.exists():
            admin.protect(path.parent)
        admin.protect(state)
        cfg = {'version': 1, 'peer_id': chosen_id, 'listen_port': selected_port,
               'state_dir': str(state), 'codex_path': selected_codex,
               'local_token': secrets.token_urlsafe(32), 'peers': {}, 'projects': {}}
    else:
        cfg = copy.deepcopy(cfg)
        cfg.update(codex_path=selected_codex, listen_port=selected_port)
    admin.save(path, cfg)
    result = {'ok': True, 'timestamp': now(), 'peer_id': chosen_id,
              'initialized': not existing, 'preserved_existing_configuration': existing,
              'python_version': '.'.join(map(str, sys.version_info[:3])),
              'codex_version': runtime.get('codex_version'),
              'runtime_compatibility': runtime.get('compatibility'),
              'authentication': auth_status(selected_codex, runner=runner),
              'next_steps': ['Pair computers with pair-setup.',
                             'Select the existing SSH route with transport-config on its owner; the Tailscale guide covers ordinary OpenSSH over Tailscale.',
                             'Select a workspace with project-select on both computers.',
                             'Optionally register owner-login startup with autostart-enable on Windows; this does not start Bridge now.',
                             'Start Bridge on both computers; start the SSH transport on its owner; run preflight.']}
    if register:
        result['registration'] = register_mcp(selected_codex, path, runner=runner)
    return result


def loopback_url(value):
    if not isinstance(value, str) or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', value):
        raise BridgeError('configuration', 'Select an http://127.0.0.1:PORT SSH forwarding endpoint.')
    _admin().port_number(int(value.rsplit(':', 1)[1]), 'peer_port')
    return value


def continue_setup(path, result, *, prompt=input):
    """One guided entry point; unfinished exchange remains resumable with no resets."""
    cfg = _admin().read_config(Path(path))
    peers = list(cfg.get('peers', {}))
    peer = peers[0] if len(peers) == 1 else ask(
        'Other computer name (leave blank to pair later)', prompt=prompt, required=False)
    if not peer:
        return result
    result['pairing'] = pair_setup(path, peer_id=peer, prompt=prompt)
    owns_route = ask('Does this computer start the existing SSH connection? yes/no',
                     'no', prompt=prompt).casefold()
    if owns_route in ('yes', 'y'):
        result['transport'] = transport_config(path, peer_id=peer, prompt=prompt)
    selected = ask('Project ID (leave blank to select later)', prompt=prompt, required=False)
    if selected:
        result['project'] = project_select(path, project_id=selected, peer_id=peer, prompt=prompt)
    return offer_autostart(path, result, prompt=prompt)


def offer_autostart(path, result, *, prompt=input, platform=None, register=None, read_settings=None):
    """Owner-only, explicit opt-in after pairing/project selection; never starts work."""
    if (platform or os.name) != 'nt':
        return result
    from . import autostart
    configured = (read_settings or autostart.settings)(path)
    entries = [*configured.get('components', {}).values(), *configured.get('transports', {}).values()]
    if any(isinstance(entry, dict) and entry.get('enabled') for entry in entries):
        result['autostart'] = {'changed': False, 'existing_registration_preserved': True,
                               'next_step': 'Use autostart-status to inspect existing startup. Add new peer transports explicitly with autostart-enable --component transport --peer PEER_ID.'}
        return result
    cfg = _admin().read_config(Path(path))
    if not cfg.get('projects') or not any(peer.get('enabled') for peer in cfg.get('peers', {}).values()):
        result['autostart'] = {'changed': False, 'offered': False,
                               'next_step': 'Complete pairing and project selection, then use autostart-enable to opt into Windows owner-login startup.'}
        return result
    choice = ask('Register Bridge and its currently enabled peer transports for your next Windows sign-in? This does not start anything now. yes/no',
                 'no', prompt=prompt).casefold()
    if choice in ('yes', 'y'):
        result['autostart'] = (register or autostart.enable)(path, component='auto')
    elif choice in ('no', 'n'):
        result['autostart'] = {'changed': False, 'offered': True, 'choice': 'not_enabled',
                               'next_step': 'You can opt in later with autostart-enable. Start Bridge manually for this login.'}
    else:
        raise BridgeError('invalid_choice', 'Startup was not changed. Answer yes or no, or run autostart-enable explicitly later.')
    return result


def _checked_invitation(path):
    try:
        path = Path(path).expanduser().resolve()
        if not path.is_file() or path.stat().st_size > 16384:
            raise ValueError('invalid invitation')
        item = _admin().read(path)
        if (not isinstance(item, dict) or item.get('protocol') != 1 or
                not isinstance(item.get('receive_token'), str) or len(item['receive_token']) < 32 or
                len(item['receive_token']) > 1024):
            raise ValueError('invalid invitation')
        identifier(item.get('peer_id'), 'peer_id')
        return item
    except (OSError, ValueError, TypeError, BridgeError) as exc:
        raise BridgeError('invitation_invalid', 'The selected file is not a valid Bridge pairing invitation.') from exc


def _next_peer_url(cfg):
    occupied = {cfg['listen_port']}
    for peer in cfg.get('peers', {}).values():
        try:
            occupied.add(int(loopback_url(peer.get('url')).rsplit(':', 1)[1]))
        except BridgeError:
            continue
    for port in range(47322, 65536):
        if port not in occupied:
            return 'http://127.0.0.1:' + str(port)
    raise BridgeError('configuration', 'Choose an unused local forwarding port explicitly.')


def pair_setup(path, *, peer_id=None, url=None, export_file=None, import_file=None,
               consume_import=False, batch=False, prompt=input):
    admin = _admin()
    path = Path(path).expanduser().resolve()
    cfg = admin.read_config(path)
    invitation = _checked_invitation(import_file) if import_file else None
    selected = peer_id or (invitation or {}).get('peer_id')
    if not selected:
        selected = ask('Name of the other computer', batch=batch, prompt=prompt)
    identifier(selected, 'peer_id')
    if selected == cfg['peer_id']:
        raise BridgeError('self_pairing', 'Pair with a different computer; each computer must have its own identity.')
    if invitation and invitation['peer_id'] != selected:
        raise BridgeError('peer_mismatch', 'The invitation belongs to a different computer. No pairing settings were changed.')
    old = cfg.get('peers', {}).get(selected, {})
    selected_url = loopback_url(url or old.get('url') or
                               ask('Local forwarded address of the other bridge', _next_peer_url(cfg), batch=batch, prompt=prompt))
    # With no explicit exchange operation, create/reuse the local export and
    # invite selection of the other file. Empty input cleanly stops at exchange.
    if not export_file and not import_file:
        export_file = path.parent / 'exchange' / ('from-' + cfg['peer_id'] + '-to-' + selected + '.json')
    if not batch and not import_file:
        candidate = ask('Received invitation file (leave blank if it has not arrived)',
                        batch=False, prompt=prompt, required=False)
        if candidate:
            import_file = Path(candidate)
            invitation = _checked_invitation(import_file)
            if invitation['peer_id'] != selected:
                raise BridgeError('peer_mismatch', 'The invitation belongs to a different computer. No pairing settings were changed.')
    peer = copy.deepcopy(old) or {'incoming_token': secrets.token_urlsafe(32),
                                 'outgoing_token': '', 'enabled': False}
    peer['url'] = selected_url
    if invitation:
        peer.update(outgoing_token=invitation['receive_token'], enabled=True)
    exported = None
    if export_file:
        exported = Path(export_file).expanduser().resolve()
        if exported == path or (import_file and exported == Path(import_file).expanduser().resolve()):
            raise BridgeError('invitation_location_denied', 'Use a separate invitation file, not the configuration or imported invitation.')
        payload = {'protocol': 1, 'peer_id': cfg['peer_id'], 'receive_token': peer['incoming_token']}
        if exported.exists():
            existing = _checked_invitation(exported)
            if existing != payload:
                raise BridgeError('invitation_exists', 'An unrelated invitation already exists at the selected location; it was preserved.')
        else:
            if not exported.parent.exists():
                admin.protect(exported.parent)
            admin.private_invitation(exported, payload)
    # Preserve ownership of an older single route before adding another peer.
    # Keep the legacy block itself intact so an existing supervisor is not
    # silently stopped just because a new computer invitation was generated.
    legacy = cfg.get('ssh_transport')
    if selected not in cfg.get('peers', {}) and legacy and not legacy.get('peer_id'):
        existing_peers = list(cfg.get('peers', {}))
        if len(existing_peers) != 1:
            raise BridgeError('configuration', 'Identify the original peer on the existing SSH transport before adding another computer.')
        cfg['ssh_transport'] = {**legacy, 'peer_id': existing_peers[0]}
    cfg.setdefault('peers', {})[selected] = peer
    admin.save(path, cfg)
    if invitation and consume_import:
        Path(import_file).expanduser().resolve().unlink()
    # The selected output filename is useful to a local owner; it contains no
    # token. General status/preflight never include these paths.
    result = {'ok': True, 'timestamp': now(), 'peer_id': selected,
              'paired_locally': bool(peer.get('enabled') and peer.get('outgoing_token')),
              'invitation_imported': bool(invitation), 'import_copy_deleted': bool(invitation and consume_import),
              'both_directions_verified': False}
    if exported:
        result['invitation_file'] = str(exported)
        result['exchange_instruction'] = 'Transfer this Bridge-only invitation privately to the named computer. Do not paste its contents into chats or commit it. Import the other computer\'s file, then delete exchange copies.'
    return result


def project_select(path, *, project_id=None, name=None, workspace=None, peer_id=None,
                   export_root=None, import_root=None, policy=None, operations=None,
                   peer_project_id=None, batch=False, prompt=input):
    admin = _admin()
    path = Path(path).expanduser().resolve()
    cfg = admin.read_config(path)
    projects = cfg.get('projects', {})
    if not project_id:
        names = ', '.join(sorted(projects)) or 'none yet'
        project_id = ask('Project ID (existing: ' + names + ')', batch=batch, prompt=prompt)
    identifier(project_id, 'project_id')
    old = projects.get(project_id, {})
    item = copy.deepcopy(old)
    selected_name = name or old.get('name') or ask('Project display name', project_id, batch=batch, prompt=prompt)
    if not isinstance(selected_name, str) or not selected_name.strip() or len(selected_name) > 200:
        raise BridgeError('configuration', 'Project display name must contain 1 to 200 characters.')
    selected_workspace = workspace or old.get('workspace')
    if not selected_workspace:
        selected_workspace = ask('Local project workspace folder', batch=batch, prompt=prompt)
    selected_workspace = Path(selected_workspace).expanduser().resolve()
    if not selected_workspace.is_dir():
        raise BridgeError('workspace_missing', 'Select an existing project workspace folder.')
    selected_peer = peer_id
    if not selected_peer:
        allowed = old.get('allowed_peers', [])
        choices = allowed or list(cfg.get('peers', {}))
        selected_peer = choices[0] if len(choices) == 1 else ask('Paired computer for this project', batch=batch, prompt=prompt)
    identifier(selected_peer, 'peer_id')
    if selected_peer not in cfg.get('peers', {}):
        raise BridgeError('pairing_required', 'Add this computer with pair-setup before selecting its project scope.')
    selected_policy = policy or old.get('policy', 'read-only')
    if selected_policy not in ('read-only', 'workspace-write'):
        raise BridgeError('configuration', 'Project policy must be read-only or workspace-write.')
    selected_ops = operations if operations is not None else old.get('allowed_ops', ['tasks', 'messages', 'artifacts', 'context'])
    if isinstance(selected_ops, str):
        selected_ops = [value.strip() for value in selected_ops.split(',') if value.strip()]
    if not isinstance(selected_ops, list) or not all(isinstance(op, str) for op in selected_ops) or not set(selected_ops) <= {'tasks', 'messages', 'artifacts', 'context'}:
        raise BridgeError('configuration', 'Project operations must be tasks, messages, artifacts, or context.')
    roots = []
    for selected, key, folder in ((export_root, 'export_root', 'bridge-export'),
                                   (import_root, 'import_root', 'bridge-import')):
        # A changed workspace must not silently keep transfer roots in the old
        # workspace. Leave those files untouched and use fresh scoped defaults.
        retained = old.get(key) if old.get('workspace') and Path(old['workspace']).resolve() == selected_workspace else None
        root = Path(selected or retained or selected_workspace / folder).expanduser().absolute()
        if not root.resolve().is_relative_to(selected_workspace) or root.resolve() == selected_workspace:
            raise BridgeError('scope_denied', 'Select a dedicated transfer folder inside the project workspace.')
        relative = root.relative_to(selected_workspace).as_posix()
        roots.append(safe_path(selected_workspace, relative))
    selected_peers = list(old.get('allowed_peers', []))
    if selected_peer not in selected_peers:
        selected_peers.append(selected_peer)
    item.update(name=selected_name, workspace=str(selected_workspace), export_root=str(roots[0]),
                import_root=str(roots[1]), allowed_peers=selected_peers,
                allowed_ops=list(dict.fromkeys(selected_ops)), policy=selected_policy)
    if peer_project_id is not None:
        item['peer_project_id'] = identifier(peer_project_id, 'peer_project_id')
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    cfg.setdefault('projects', {})[project_id] = item
    admin.save(path, cfg)
    scope_changed = bool(old and any(old.get(key) != item.get(key) for key in
                                    ('workspace', 'policy', 'allowed_ops', 'peer_project_id')))
    return {'ok': True, 'timestamp': now(), 'project_id': project_id, 'name': selected_name,
            'peer_id': selected_peer, 'policy': selected_policy, 'allowed_ops': item['allowed_ops'],
            'updated': bool(old), 'preserved_local_actions': 'local_actions' in old,
            'new_session_required': scope_changed,
            'next_step': 'Select this project ID with this computer\'s own workspace on the other computer, then create a session.'}


def transport_config(path, *, peer_id=None, settings=None, batch=False, prompt=input):
    from .transport import configure_transport, validate_transports
    admin = _admin()
    path = Path(path).expanduser().resolve()
    cfg = admin.read_config(path)
    if not peer_id:
        peers = list(cfg.get('peers', {}))
        peer_id = peers[0] if len(peers) == 1 else ask('Computer reached through this SSH connection', batch=batch, prompt=prompt)
    identifier(peer_id, 'peer_id')
    if peer_id not in cfg.get('peers', {}):
        raise BridgeError('pairing_required', 'Add this computer with pair-setup before selecting its SSH route.')
    current = cfg.get('ssh_transports', {}).get(peer_id, {})
    if not current and cfg.get('ssh_transport') and len(cfg.get('peers', {})) == 1:
        current = cfg['ssh_transport']
    supplied = settings or {}
    item = copy.deepcopy(current)
    labels = {'ssh_host': 'Existing SSH host or address', 'username': 'Existing SSH account',
              'identity_file': 'Existing local SSH identity file', 'known_hosts_file': 'Existing local verified known-hosts file',
              'host_key_alias': 'Verified SSH host-key alias'}
    defaults = {'ssh_exe': shutil.which('ssh'), 'ssh_port': 22,
                'local_peer_port': int(loopback_url(cfg['peers'][peer_id]['url']).rsplit(':', 1)[1]),
                'remote_bridge_port': 47321, 'remote_peer_port': 47322}
    for key in ('ssh_host', 'username', 'identity_file', 'known_hosts_file', 'host_key_alias',
                'ssh_exe', 'ssh_port', 'local_peer_port', 'remote_bridge_port', 'remote_peer_port'):
        value = supplied.get(key, current.get(key))
        if value is None:
            value = ask(labels.get(key, key.replace('_', ' ')), defaults.get(key), batch=batch, prompt=prompt)
        if key.endswith('_port'):
            try:
                value = int(value)
            except (ValueError, TypeError) as exc:
                raise BridgeError('configuration', 'SSH and forwarding ports must be integers.') from exc
        if key in ('identity_file', 'known_hosts_file'):
            value = str(Path(value).expanduser().resolve())
        item[key] = value
    item['enabled'] = True
    updated = configure_transport(cfg, peer_id, item)
    validate_transports(updated)
    updated['peers'][peer_id]['url'] = 'http://127.0.0.1:' + str(item['local_peer_port'])
    admin.save(path, updated)
    return {'ok': True, 'timestamp': now(), 'peer_id': peer_id, 'configured': True,
            'started': False, 'private_keys_copied': False,
            'next_step': 'Start both bridges, then transport-start --peer ' + peer_id + ' on this computer. Keep the other computer\'s peer URL matched to remote_peer_port.'}


def peer_probe(peer, *, timeout=5):
    """Probe only the configured loopback forwarding endpoint with its credential."""
    request = urllib.request.Request(loopback_url(peer['url']) + '/rpc',
        data=canonical({'method': 'peer.status', 'params': {}}).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + peer['outgoing_token']})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as reply:
            raw = reply.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return {'ok': False, 'code': 'protocol_error'}
        result = json.loads(raw)
        if result.get('ok'):
            status = result.get('result', {})
            projects = status.get('projects', [])
            return {'ok': True, 'peer_id': status.get('peer_id'),
                    'runtime_ready': status.get('codex', {}).get('ready'),
                    'projects': [{'project_id': item.get('project_id'),
                                  'allowed_ops': [op for op in item.get('allowed_ops', [])
                                                  if op in ('tasks', 'messages', 'artifacts', 'context')]}
                                 for item in projects if isinstance(item, dict)]}
        return {'ok': False, 'code': result.get('error', {}).get('code', 'protocol_error')}
    except urllib.error.HTTPError as exc:
        return {'ok': False, 'code': 'unauthorized' if exc.code in (401, 403) else 'protocol_error'}
    except (OSError, urllib.error.URLError, TimeoutError):
        return {'ok': False, 'code': 'peer_unavailable'}
    except (ValueError, TypeError, AttributeError):
        return {'ok': False, 'code': 'protocol_error'}


def _tcp_probe(host, port):
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def preflight(path, *, peer_id=None, project_id=None, runtime_probe=inspect_runtime,
              runner=_run, probe=peer_probe, tcp_probe=_tcp_probe, local_call=None):
    """Sanitized layered diagnostics: no configuration, paths, logs, or raw errors."""
    admin = _admin()
    checks = []
    def add(category, state, code, message, **safe):
        checks.append({'category': category, 'state': state, 'code': code, 'message': message, **safe})
    try:
        cfg = admin.read_config(Path(path).expanduser().resolve())
    except Exception:
        add('configuration', 'fail', 'configuration_invalid', 'Run setup with a valid local configuration first.')
        return {'ok': False, 'timestamp': now(), 'checks': checks}
    add('configuration', 'pass', 'configuration_loaded', 'The local Bridge configuration is readable.')
    add('python', 'pass' if sys.version_info >= (3, 11) else 'fail', 'python_version',
        'Python 3.11 or newer is required.', version='.'.join(map(str, sys.version_info[:3])))
    try:
        runtime = runtime_probe(cfg['codex_path'])
    except Exception:
        runtime = {'ready': False}
    add('runtime', 'pass' if runtime.get('ready') else 'fail',
        'runtime_compatible' if runtime.get('ready') else 'runtime_incompatible',
        'Codex App Server schema is compatible.' if runtime.get('ready') else 'Codex App Server compatibility could not be verified.',
        version=runtime.get('codex_version'))
    auth = auth_status(cfg['codex_path'], runner=runner)
    add('authentication', auth['state'], auth['code'], auth['message'])
    try:
        local = (local_call or admin.call)(Path(path), 'session_list', {}, timeout=3)
        if local.get('ok'):
            add('local_bridge', 'pass', 'bridge_running', 'The local Bridge responds with its local credential.')
        else:
            add('local_bridge', 'fail', 'local_credential_rejected', 'A local endpoint rejected this Bridge configuration.')
    except Exception:
        add('local_bridge', 'fail', 'bridge_not_running', 'Start this computer\'s Bridge before sending requests.')
    peer_ids = [peer_id] if peer_id else list(cfg.get('peers', {}))
    if not peer_ids:
        add('pairing', 'fail', 'pairing_required', 'Run pair-setup on both computers.')
    for selected in peer_ids:
        peer = cfg.get('peers', {}).get(selected)
        safe = {'peer_id': selected}
        if not peer or not peer.get('enabled') or len(peer.get('outgoing_token', '')) < 32:
            add('pairing', 'fail', 'pairing_incomplete', 'Import the other computer\'s private invitation on this computer.', **safe)
            continue
        try:
            reachable = probe(peer)
        except Exception:
            reachable = {'ok': False, 'code': 'peer_unavailable'}
        if reachable.get('ok') and reachable.get('peer_id') == selected:
            add('forwarding', 'pass', 'peer_route_ready', 'The configured forwarded Bridge endpoint responds.', **safe)
            add('pairing', 'pass', 'peer_authenticated', 'The expected paired computer accepted the Bridge credential.', **safe)
            if reachable.get('runtime_ready') is not None:
                add('peer_runtime', 'pass' if reachable['runtime_ready'] else 'fail',
                    'peer_runtime_ready' if reachable['runtime_ready'] else 'peer_runtime_unavailable',
                    'The other computer reports its Codex runtime is ready.' if reachable['runtime_ready'] else
                    'Run preflight locally on the other computer to check its Codex runtime and sign-in.', **safe)
        elif reachable.get('ok') or reachable.get('code') in ('unauthorized', 'access_revoked', 'peer_mismatch'):
            add('forwarding', 'pass', 'endpoint_reachable', 'The forwarded endpoint is reachable.', **safe)
            add('pairing', 'fail', 'peer_identity_or_credential', 'The peer identity or pairing credential does not match; verify both invitation imports.', **safe)
        elif reachable.get('code') in ('unsupported_codex_schema', 'unsupported_codex_version',
                'runtime_probe_failed', 'codex_not_found', 'authentication_required', 'not_logged_in'):
            auth_failure = reachable.get('code') in ('authentication_required', 'not_logged_in')
            add('forwarding', 'pass', 'endpoint_reachable', 'The other Bridge responds through the configured forward.', **safe)
            add('pairing', 'pass', 'peer_credential_accepted', 'The peer accepted the credential before checking its runtime.', **safe)
            add('peer_authentication' if auth_failure else 'peer_runtime', 'fail',
                'peer_sign_in_required' if auth_failure else 'peer_runtime_incompatible',
                'Run preflight locally on the other computer to repair its Codex runtime or sign-in.', **safe)
        else:
            code = reachable.get('code')
            add('forwarding', 'fail', 'forwarding_unavailable' if code == 'peer_unavailable' else 'forwarding_protocol',
                'The configured forward does not reach a Bridge. Check both daemons, matching ports, and local/reverse SSH forwarding.', **safe)
            add('pairing', 'unknown', 'pairing_unverified', 'Pairing cannot be tested until the forwarded endpoint responds.', **safe)
        try:
            from .transport import configured_transports
            route = configured_transports(cfg).get(selected)
        except BridgeError:
            add('ssh', 'fail', 'ssh_configuration_invalid', 'Identify the peer for each existing SSH transport before testing additional computers.', **safe)
            route = None
        if route:
            if reachable.get('ok'):
                add('ssh', 'pass', 'ssh_route_operational', 'The configured Bridge route is operational.', **safe)
            else:
                try:
                    admin.transport_args({**cfg, 'ssh_transport': route})
                    from .transport import transport_status
                    state = transport_status(path, selected)
                    saved = next((item for item in state['transports'] if item['peer_id'] == selected), {})
                    last_error = saved.get('last_error')
                    code = last_error.get('code') if isinstance(last_error, dict) else last_error
                    failures = {
                        'ssh_authentication_failed': ('ssh', 'SSH rejected the configured identity. Verify the existing authorized SSH connection locally.'),
                        'ssh_host_key_failed': ('ssh', 'SSH rejected the host key. Verify the existing known-hosts entry locally.'),
                        'forwarding_port_in_use': ('forwarding', 'SSH reported a forwarding port already in use. Select unused Bridge ports without changing other application routes.'),
                        'forwarding_denied': ('forwarding', 'The SSH server denied forwarding. The existing SSH route must permit both local and reverse forwarding.'),
                        'ssh_endpoint_unreachable': ('ssh', 'SSH reported that the configured endpoint is unreachable. Restore the existing tunnel or connection.')}
                    if code in failures:
                        category, message = failures[code]
                        add(category, 'fail', code, 'Last transport attempt: ' + message, **safe)
                    else:
                        ssh_reachable = tcp_probe(route['ssh_host'], route['ssh_port'])
                        add('ssh', 'unknown' if ssh_reachable else 'fail',
                            'ssh_endpoint_reachable' if ssh_reachable else 'ssh_endpoint_unreachable',
                            'The SSH endpoint accepts TCP; start the configured transport to verify authentication and forwarding.' if ssh_reachable else
                            'The configured SSH endpoint is unreachable. Restore the existing SSH or tunnel connection.', **safe)
                except Exception:
                    add('ssh', 'fail', 'ssh_configuration_invalid', 'Complete transport-config with the existing SSH identity and verified known-hosts files.', **safe)
        else:
            add('ssh', 'pass' if reachable.get('ok') else 'unknown',
                'peer_managed_route_ready' if reachable.get('ok') else 'peer_managed_route',
                'The other computer or an existing local tunnel owns this route.', **safe)
        if project_id:
            project = cfg.get('projects', {}).get(project_id)
            valid = bool(project and selected in project.get('allowed_peers', []) and
                         Path(project.get('workspace', '')).is_dir() and
                         project.get('policy') in ('read-only', 'workspace-write'))
            add('project_scope', 'pass' if valid else 'fail', 'project_selected' if valid else 'project_scope_missing',
                'This computer has selected the project workspace for this peer.' if valid else
                'Run project-select locally with this project ID and paired computer.', project_id=project_id, **safe)
            if reachable.get('ok'):
                remote_id = project.get('peer_project_id', project_id) if project else project_id
                remote_project = next((item for item in reachable.get('projects', [])
                                       if item.get('project_id') == remote_id), None)
                add('peer_project_scope', 'pass' if remote_project else 'fail',
                    'peer_project_selected' if remote_project else 'peer_project_scope_missing',
                    'The other computer exposes the selected project to this pairing.' if remote_project else
                    'Run project-select on the other computer for this pairing and project.', project_id=remote_id, **safe)
    return {'ok': all(item['state'] == 'pass' for item in checks), 'timestamp': now(),
            'peer_id': cfg['peer_id'], 'checks': checks,
            'note': 'This is a local read-only diagnostic. Run it on both computers to verify both directions. It does not execute a Codex task.'}


def show_chat(path, session_id, *, open_chat=False, caller=None, opener=None):
    admin = _admin()
    identifier(session_id, 'session_id')
    response = admin.valid_response((caller or admin.call)(Path(path), 'session_chat', {'session_id': session_id}))
    result = response.get('result', response)
    if open_chat:
        uri = result.get('chat_url') or result.get('uri') or result.get('local_chat_uri') or result.get('conversation_uri')
        if not isinstance(uri, str) or not re.fullmatch(r'codex://threads/[A-Za-z0-9_.-]+', uri):
            raise BridgeError('chat_not_ready', 'The local project conversation is not available yet. Send this computer its first session task.')
        if result.get('can_open') is False:
            raise BridgeError('chat_not_released', 'The Bridge has not confirmed releasing this conversation. Check its ownership state before opening it in the desktop.')
        if opener:
            opener(uri)
        elif os.name == 'nt':
            os.startfile(uri)
        else:
            launcher = 'open' if sys.platform == 'darwin' else 'xdg-open'
            subprocess.run([launcher, uri], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result = {**result, 'open_requested': True}
    return result
