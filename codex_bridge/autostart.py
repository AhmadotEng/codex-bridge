"""Independent owner-login startup: waiting daemon or one explicit persistent peer."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from .core import BridgeError, canonical, identifier, now, windows_file_retry
from . import processes

COMPONENTS = ('daemon', 'transport')
RESTART_DELAYS = (1, 2, 4, 8, 15)
STABLE_SECONDS = 60

def _cli():
    from . import cli
    return cli

def state_path(path): return Path(_cli().read_config(path)['state_dir'])

def settings(path):
    selected = state_path(path)/'autostart.json'
    data = _cli().read(selected) if selected.exists() else {'version': 2, 'components': {}, 'transports': {}}
    if data.get('version') not in (1, 2) or not isinstance(data.get('components'), dict) or not isinstance(data.get('transports', {}), dict):
        raise BridgeError('configuration', 'Invalid local autostart settings')
    data.setdefault('transports', {})
    return data

def _validate(component, peer_id=None):
    if component not in COMPONENTS: raise BridgeError('configuration', 'Select daemon or transport')
    if peer_id is not None:
        identifier(peer_id, 'peer_id')
        if component != 'transport': raise BridgeError('configuration', '--peer applies only to transport startup')

def _entry_settings(data, component, peer_id=None):
    _validate(component, peer_id)
    return data['transports'].get(peer_id, {}) if peer_id is not None else data['components'].get(component, {})

def enabled(path, component, peer_id=None): return bool(_entry_settings(settings(path), component, peer_id).get('enabled'))

def task_name(path, component, peer_id=None):
    _validate(component, peer_id)
    config_identity = str(Path(path).resolve())
    suffix = hashlib.sha256((config_identity.casefold() if os.name == 'nt' else config_identity).encode()).hexdigest()[:16]
    label = component if peer_id is None else component+'-'+hashlib.sha256(peer_id.encode()).hexdigest()[:16]
    return f'CodexBridgeStartup-{label}-{suffix}'

def component_directory(path, component, peer_id=None):
    _validate(component, peer_id)
    return state_path(path)/'transports'/peer_id if peer_id is not None else state_path(path)

def stop_path(path, component, peer_id=None):
    return component_directory(path, component, peer_id)/('stop' if peer_id else 'serve.stop' if component == 'daemon' else 'transport.stop')

def runner_record(path, component, peer_id=None):
    return component_directory(path, component, peer_id)/('autostart.pid.json' if peer_id else f'autostart-{component}.pid.json')

def runner_lock(path, component, peer_id=None):
    return component_directory(path, component, peer_id)/('autostart.lock' if peer_id else f'autostart-{component}.lock')

def component_lock(path, component, peer_id=None):
    return component_directory(path, component, peer_id)/('supervisor.lock' if peer_id else 'serve.lock' if component == 'daemon' else 'transport.lock')

def request_stop(path, component, peer_id=None):
    cli = _cli(); record_path = runner_record(path, component, peer_id)
    try: record = cli.read(record_path) if record_path.exists() else {}
    except (ValueError, OSError): record = {}
    login = record.get('login_identity') if processes.matches(record) else None
    if not login and not processes.interactive_session():
        with contextlib.suppress(ValueError, OSError): login = cli.read(stop_path(path, component, peer_id)).get('login_identity')
    if not login: login = processes.login_identity()
    cli.save(stop_path(path, component, peer_id), {'requested_at': now(), 'login_identity': login})

def clear_stop(path, component, peer_id=None):
    windows_file_retry(lambda: stop_path(path, component, peer_id).unlink(missing_ok=True))

def _windows():
    if os.name != 'nt': raise BridgeError('configuration', 'Persistent owner-login tasks require Windows; use manual background startup on this platform')

def _uses_windows(): return os.name == 'nt'

def _targets(path, component, peer_id=None, *, for_enable=False):
    if component not in ('daemon', 'transport', 'auto', 'all'): raise BridgeError('configuration', 'Select daemon, transport, auto, or all')
    if component in ('daemon', 'auto') and peer_id is not None: raise BridgeError('configuration', '--peer applies only to explicit transport startup')
    if peer_id is not None: identifier(peer_id, 'peer_id')
    result = [('daemon', None)] if component in ('daemon', 'auto', 'all') else []
    # Legacy "auto" means waiting only. Never infer outgoing demand from pairing.
    if component in ('daemon', 'auto'): return result
    if for_enable:
        if component != 'transport' or peer_id is None:
            raise BridgeError('persistent_peer_required', 'Enable waiting startup with --component daemon. Persistent connections require --component transport --peer PEER_ID explicitly.')
        from .transport import configured_transports, transport_args
        cfg = _cli().read_config(path); routes = configured_transports(cfg)
        peers = [peer_id] if peer_id is not None else sorted(k for k, v in routes.items() if v.get('enabled') and cfg['peers'][k].get('enabled'))
        if component == 'transport' and not peers: raise BridgeError('configuration', 'Configure and enable a paired transport before enabling login startup')
        for peer in peers:
            if not routes.get(peer, {}).get('enabled') or not cfg.get('peers', {}).get(peer, {}).get('enabled'):
                raise BridgeError('configuration', 'The selected transport and pairing must be enabled')
            transport_args(cfg, peer); result.append(('transport', peer))
    else:
        data = settings(path)
        peers = [peer_id] if peer_id is not None else sorted(data['transports'])
        result.extend(('transport', peer) for peer in peers)
        if peer_id is None and 'transport' in data['components']: result.append(('transport', None))
    return result

def _pythonw():
    candidate = Path(sys.executable).resolve().with_name('pythonw.exe')
    if not candidate.is_file(): raise BridgeError('configuration', 'The selected Python installation needs pythonw.exe for hidden owner-login startup')
    return candidate

def _registration(path, component, peer_id=None):
    arguments = ['-m', 'codex_bridge.cli', '--config', str(path), 'autostart-run', '--component', component]
    if peer_id is not None: arguments.extend(['--peer', peer_id])
    return {'enabled': True, 'component': component, 'peer_id': peer_id, 'task_name': task_name(path, component, peer_id),
            'python': str(_pythonw()), 'arguments': subprocess.list2cmdline(arguments), 'source': str(Path(__file__).resolve().parents[1])}

def _owner_guard(path, component, peer_id, data):
    """Validate the exact saved owner action before any Windows task mutation."""
    cli = _cli()
    saved = _entry_settings(data, component, peer_id)
    arguments = ['-m', 'codex_bridge.cli', '--config', str(path), 'autostart-run', '--component', component]
    if peer_id is not None: arguments.extend(['--peer', peer_id])
    return ['$task = Get-ScheduledTask -TaskName ' + cli.ps_literal(task_name(path, component, peer_id)) + ' -ErrorAction SilentlyContinue',
        'if ($task) {',
        '$owner = if ($task.Principal.UserId -match \'^S-1-\') { [Security.Principal.SecurityIdentifier]::new($task.Principal.UserId).Value } else { [Security.Principal.NTAccount]::new($task.Principal.UserId).Translate([Security.Principal.SecurityIdentifier]).Value }',
        'if ($owner -ne $identity.User.Value -or @($task.Actions).Count -ne 1 -or ' +
        '$task.Actions[0].Execute -ne ' + cli.ps_literal(saved.get('python', str(Path(sys.executable).resolve().with_name('pythonw.exe')))) + ' -or ' +
        '$task.Actions[0].Arguments -ne ' + cli.ps_literal(saved.get('arguments', subprocess.list2cmdline(arguments))) + ' -or ' +
        '$task.Actions[0].WorkingDirectory -ne ' + cli.ps_literal(saved.get('source', str(Path(__file__).resolve().parents[1]))) +
        ") { throw 'Existing startup task does not match the saved Bridge owner and launcher; it was preserved.' }", '}']

def _transaction_begin(names):
    cli = _cli()
    names = '@(' + ','.join(cli.ps_literal(name) for name in names) + ')'
    return ['$previousTasks = @()', 'foreach ($name in ' + names + ') {',
            '$task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue',
            '$previousTasks += [pscustomobject]@{Name=$name; Xml=if($task){Export-ScheduledTask -TaskName $name}else{$null}}',
            '}', 'try {']

def _transaction_end():
    return ['} catch {', '$originalFailure = $_; $rollbackFailed = $false',
            'foreach ($previous in $previousTasks) { try {',
            'if ($previous.Xml) { Register-ScheduledTask -TaskName $previous.Name -Xml $previous.Xml -Force | Out-Null }',
            'elseif (Get-ScheduledTask -TaskName $previous.Name -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $previous.Name -Confirm:$false }',
            '} catch { $rollbackFailed = $true } }',
            "if ($rollbackFailed) { throw 'Startup registration failed and rollback was incomplete. Inspect the exact Bridge tasks; active work was not stopped.' }",
            'throw $originalFailure', '}']

def enable(path, component='auto', peer_id=None):
    if not _uses_windows():
        from . import autostart_posix
        return autostart_posix.enable(path, component, peer_id)
    _windows(); cli = _cli(); selected = _targets(path, component, peer_id, for_enable=True)
    entries = [_registration(path, kind, peer) for kind, peer in selected]; data = settings(path)
    migrate_legacy = any(kind == 'transport' for kind, peer in selected) and bool(data['components'].get('transport', {}).get('enabled'))
    lines = ["$ErrorActionPreference = 'Stop'", '$identity = [Security.Principal.WindowsIdentity]::GetCurrent()']
    for item in entries:
        lines += ['$task = Get-ScheduledTask -TaskName '+cli.ps_literal(item['task_name'])+' -ErrorAction SilentlyContinue',
            'if ($task) {',
            '$owner = if ($task.Principal.UserId -match \'^S-1-\') { [Security.Principal.SecurityIdentifier]::new($task.Principal.UserId).Value } else { [Security.Principal.NTAccount]::new($task.Principal.UserId).Translate([Security.Principal.SecurityIdentifier]).Value }',
            '$ownerTriggers = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq \'MSFT_TaskLogonTrigger\' -and ($_.UserId -eq $identity.Name -or $_.UserId -eq $identity.User.Value) })',
            'if ($owner -ne $identity.User.Value -or [string]$task.Principal.LogonType -ne \'Interactive\' -or [string]$task.Principal.RunLevel -ne \'Limited\' -or -not $task.Settings.Hidden -or @($task.Actions).Count -ne 1 -or @($task.Triggers).Count -ne 1 -or $ownerTriggers.Count -ne 1 -or '+
            '$task.Actions[0].Execute -ne '+cli.ps_literal(item['python'])+' -or '+'$task.Actions[0].Arguments -ne '+cli.ps_literal(item['arguments'])+' -or '+'$task.Actions[0].WorkingDirectory -ne '+cli.ps_literal(item['source'])+
            ") { throw 'Existing startup registration differs from the expected owner or launcher; inspect it before changing it.' }", '}']
    if migrate_legacy: lines += _owner_guard(path, 'transport', None, data)
    names = [item['task_name'] for item in entries] + ([task_name(path, 'transport')] if migrate_legacy else [])
    lines += _transaction_begin(names)
    if migrate_legacy:
        legacy = cli.ps_literal(task_name(path, 'transport'))
        lines += ['if (Get-ScheduledTask -TaskName '+legacy+' -ErrorAction SilentlyContinue) { Disable-ScheduledTask -TaskName '+legacy+' | Out-Null }']
    for item in entries:
        name = cli.ps_literal(item['task_name'])
        lines += ['$task = Get-ScheduledTask -TaskName '+name+' -ErrorAction SilentlyContinue',
            "if ($task -and $task.State -eq 'Running') { Enable-ScheduledTask -TaskName "+name+' | Out-Null } else {',
            '$action = New-ScheduledTaskAction -Execute '+cli.ps_literal(item['python'])+' -Argument '+cli.ps_literal(item['arguments'])+' -WorkingDirectory '+cli.ps_literal(item['source']),
            '$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited',
            '$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name',
            '$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries'+(' -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)' if item['component'] == 'daemon' else ''),
            'Register-ScheduledTask -TaskName '+name+' -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Description '+cli.ps_literal('Codex Bridge owner-login startup; local account, no password or elevation.')+' -Force | Out-Null', '}']
    lines += _transaction_end()
    cli.powershell('\n'.join(lines))
    for item in entries:
        (data['transports'] if item['peer_id'] is not None else data['components'])[item['peer_id'] or item['component']] = item
    if migrate_legacy: data['components']['transport']['enabled'] = False
    data.update(version=2, updated_at=now()); cli.save(state_path(path)/'autostart.json', data)
    return {'enabled_components': [{'component': kind, 'peer_id': peer} for kind, peer in selected], 'started_now': False,
            'legacy_transport_disabled': migrate_legacy, 'tasks': [item['task_name'] for item in entries],
            'note': 'Runs after this Windows owner signs in. Daemon startup only waits; explicit per-peer persistent startup consults the finite connection manager. Registration does not start anything now.'}

def disable(path, component='all', remove=False, peer_id=None):
    if not _uses_windows():
        from . import autostart_posix
        return autostart_posix.disable(path, component, remove, peer_id)
    _windows(); cli = _cli(); selected = _targets(path, component, peer_id); data = settings(path)
    lines = ["$ErrorActionPreference = 'Stop'", '$identity = [Security.Principal.WindowsIdentity]::GetCurrent()']
    for kind, peer in selected: lines += _owner_guard(path, kind, peer, data)
    if remove:
        for kind, peer in selected:
            if cli.lock_held(runner_lock(path, kind, peer)) or cli.lock_held(component_lock(path, kind, peer)):
                raise BridgeError('component_running', 'Stop the selected component before removing its login task')
            lines += ['$task = Get-ScheduledTask -TaskName '+cli.ps_literal(task_name(path, kind, peer))+' -ErrorAction SilentlyContinue',
                "if ($task -and $task.State -eq 'Running') { throw 'Stop the selected task before removing its registration.' }"]
    lines += _transaction_begin([task_name(path, kind, peer) for kind, peer in selected])
    for kind, peer in selected:
        literal = cli.ps_literal(task_name(path, kind, peer))
        lines += ['if (Get-ScheduledTask -TaskName '+literal+' -ErrorAction SilentlyContinue) {',
            ('Unregister-ScheduledTask -TaskName '+literal+' -Confirm:$false' if remove else 'Disable-ScheduledTask -TaskName '+literal+' | Out-Null'), '}']
    lines += _transaction_end()
    cli.powershell('\n'.join(lines))
    for kind, peer in selected:
        mapping, key = (data['transports'], peer) if peer is not None else (data['components'], kind)
        mapping.setdefault(key, {'task_name': task_name(path, kind, peer)}).update(enabled=False, registration_removed=remove)
    data.update(version=2, updated_at=now()); cli.save(state_path(path)/'autostart.json', data)
    return {'disabled_components': [{'component': kind, 'peer_id': peer} for kind, peer in selected], 'registrations_removed': remove, 'running_work_stopped': False}

def status(path, peer_id=None):
    if not _uses_windows():
        from . import autostart_posix
        return autostart_posix.status(path, peer_id)
    _windows(); cli = _cli(); selected = _targets(path, 'all', peer_id)
    lines = ["$ErrorActionPreference = 'Stop'", '$rows = @()']
    for kind, peer in selected:
        lines += ['$task = Get-ScheduledTask -TaskName '+cli.ps_literal(task_name(path, kind, peer))+' -ErrorAction SilentlyContinue',
            '$info = if ($task) { Get-ScheduledTaskInfo -TaskName $task.TaskName } else { $null }',
            '$rows += [pscustomobject]@{component='+cli.ps_literal(kind)+'; peer_id='+('$null' if peer is None else cli.ps_literal(peer))+'; registered=[bool]$task; state=if($task){[string]$task.State}else{$null}; enabled=if($task){[bool]$task.Settings.Enabled}else{$false}; logon_type=if($task){[string]$task.Principal.LogonType}else{$null}; run_level=if($task){[string]$task.Principal.RunLevel}else{$null}; hidden=if($task){[bool]$task.Settings.Hidden}else{$false}; last_result=if($info){$info.LastTaskResult}else{$null}}']
    lines.append('ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress')
    rows = json.loads(cli.powershell('\n'.join(lines))); data = settings(path)
    for row in rows:
        kind, peer = row['component'], row.get('peer_id')
        row.update(configured_enabled=bool(_entry_settings(data, kind, peer).get('enabled')), task_name=task_name(path, kind, peer),
            stop_requested=stop_path(path, kind, peer).exists(), runner=cli.record_status(runner_record(path, kind, peer), runner_lock(path, kind, peer)), legacy_unbound=kind == 'transport' and peer is None)
    return {'components': rows, 'readiness': 'Login-task/process state does not verify SSH, peer reachability, or Codex task completion.'}

def start(path, component, peer_id=None):
    if not _uses_windows():
        from . import autostart_posix
        return autostart_posix.start(path, component, peer_id)
    _windows()
    if component == 'transport' and peer_id is None: raise BridgeError('legacy_autostart_requires_peer', 'Re-enable transport startup with an explicit --peer before starting the legacy login task')
    if not enabled(path, component, peer_id): raise BridgeError('configuration', 'Persistent startup is not enabled for this component')
    cli = _cli(); name = task_name(path, component, peer_id)
    cli.powershell('\n'.join(["$ErrorActionPreference = 'Stop'", '$identity = [Security.Principal.WindowsIdentity]::GetCurrent()', *_owner_guard(path, component, peer_id, settings(path)), '$task = Get-ScheduledTask -TaskName '+cli.ps_literal(name)+' -ErrorAction SilentlyContinue',
        "if (-not $task -or $task.State -eq 'Disabled') { throw 'The configured login task is missing or disabled; run autostart-enable to repair it.' }", 'Start-ScheduledTask -TaskName '+cli.ps_literal(name)]))
    return {'launch_mode': 'owner-login-task', 'task_name': name, 'peer_id': peer_id, 'start_requested': True}

def _event(stream, event, **fields):
    stream.write(canonical({'event': event, 'timestamp': now(), **fields})+'\n'); stream.flush()

@contextlib.contextmanager
def marker_control(path, component, peer_id=None):
    cli = _cli()
    if component == 'daemon':
        with cli.daemon_control(path): yield
        return
    lock = component_directory(path, component, peer_id)/'start.lock'
    deadline = time.monotonic()+5
    while True:
        manager = cli.process_lock(lock)
        try: manager.__enter__(); break
        except BridgeError as exc:
            if exc.code != 'already_running' or time.monotonic() >= deadline: raise
            time.sleep(.05)
    try: yield
    finally: manager.__exit__(None, None, None)

def run(path, component, peer_id=None):
    cli = _cli(); directory = component_directory(path, component, peer_id); directory.mkdir(parents=True, exist_ok=True)
    record_path = runner_record(path, component, peer_id)
    with cli.process_lock(runner_lock(path, component, peer_id)):
        if not enabled(path, component, peer_id): return 0
        login = processes.login_identity(); record = processes.record(os.getpid(), created_at=now(), login_identity=login)
        cli.save(record_path, record)
        try:
            with (directory/('autostart.log' if peer_id else f'autostart-{component}.log')).open('a', encoding='utf-8') as log:
                try:
                    if component == 'transport' and peer_id is None: raise BridgeError('legacy_autostart_requires_peer', 'Re-enable transport startup with --peer; the old task has no safe fixed peer scope')
                    with marker_control(path, component, peer_id):
                        marker = stop_path(path, component, peer_id)
                        if marker.exists():
                            # New sign-in may resume the waiting daemon; it must
                            # never undo a deliberate per-peer disconnect.
                            if component == 'transport': return 0
                            try: previous = cli.read(marker)
                            except (ValueError, OSError): return 0
                            if not isinstance(previous, dict) or not previous.get('login_identity'): return 0
                            if previous.get('login_identity') == login: return 0
                            clear_stop(path, component, peer_id)
                    return supervise(path, component, log, peer_id)
                except Exception as exc:
                    error = exc.as_dict() if isinstance(exc, BridgeError) else {'code': 'startup_failed', 'message': 'The selected component could not start; inspect local component logs'}
                    _event(log, 'startup_failed', component=component, peer_id=peer_id, error=error); return 1
        finally:
            with contextlib.suppress(OSError, ValueError):
                if cli.read(record_path) == record: windows_file_retry(lambda: record_path.unlink(missing_ok=True))

def supervise(path, component, log, peer_id=None):
    cli = _cli(); state = component_directory(path, component, peer_id); state.mkdir(parents=True, exist_ok=True)
    if component == 'transport' and peer_id is None: raise BridgeError('legacy_autostart_requires_peer', 'Select an explicit peer for transport startup')
    if component == 'transport':
        # One durable demand, not an SSH subprocess/restart loop. The daemon owns
        # retry budgets across scheduler retries, restarts and Windows sign-ins.
        if stop_path(path, 'daemon').exists():
            _event(log, 'local_daemon_stopped', component=component, peer_id=peer_id)
            return 0
        try:
            cli.valid_response(cli.call(path, 'session_list', {}, timeout=2))
        except OSError:
            cli.start_daemon(path)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'codex-bridge-persistent:' + str(Path(path).resolve()) + ':' + peer_id))
        result = cli.valid_response(cli.call(path, 'connection_ensure',
            {'peer_id': peer_id, 'request_id': request_id, 'persistent': True}, timeout=50))
        _event(log, 'persistent_demand_submitted', component=component, peer_id=peer_id, request_id=request_id)
        return 0
    mode = 'serve'
    if cli.lock_held(component_lock(path, component, peer_id)):
        _event(log, 'component_already_running', component=component, peer_id=peer_id); return 0
    marker = stop_path(path, component, peer_id); source = str(Path(__file__).resolve().parents[1])
    arguments = [sys.executable, '-m', 'codex_bridge.cli', '--config', str(path), mode]
    if peer_id is not None: arguments.extend(['--peer', peer_id])
    env = os.environ.copy(); env['PYTHONPATH'] = source; failures = 0
    while not marker.exists():
        started = time.monotonic()
        with (state/(mode+'.stdout.log')).open('ab') as out, (state/(mode+'.stderr.log')).open('ab') as err:
            owner = processes.OwnedProcess(arguments, cwd=source, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err); child = owner.process
            _event(log, 'component_started', component=component, peer_id=peer_id, pid=child.pid)
            try:
                stopping = None
                while child.poll() is None:
                    if marker.exists():
                        if stopping is None: stopping = time.monotonic()
                        if component == 'daemon':
                            with contextlib.suppress(OSError, BridgeError): cli.call(path, 'shutdown', timeout=1)
                        if time.monotonic()-stopping >= 15:
                            _event(log, 'component_stop_timeout', component=component, peer_id=peer_id); break
                    time.sleep(.25)
            finally: owner.close()
            code = child.poll()
        _event(log, 'component_exited', component=component, peer_id=peer_id, exit_code=code)
        if marker.exists() or code == 0: return 0
        if time.monotonic()-started >= STABLE_SECONDS: failures = 0
        if failures >= len(RESTART_DELAYS):
            _event(log, 'component_restart_exhausted', component=component, failures=failures); return 1
        delay = RESTART_DELAYS[failures]; failures += 1
        _event(log, 'component_restart_wait', component=component, seconds=delay, attempt=failures)
        deadline = time.monotonic()+delay
        while not marker.exists() and time.monotonic() < deadline: time.sleep(.25)
    return 0
