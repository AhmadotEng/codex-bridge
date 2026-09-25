"""Independent, loopback-only SSH supervisors for explicitly paired peers.

Pairing credentials still live in the bridge config. This module only owns its
own forwarding subprocesses, never a host shell or another application's tunnel.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from .core import BridgeError, canonical, identifier, now
from . import processes


def classify_ssh_error(stderr: str | bytes) -> str:
    """Reduce private SSH diagnostics to a safe actionable category."""
    value = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    value = value.casefold()
    if any(text in value for text in ("host key verification failed", "remote host identification has changed", "no matching host key")):
        return "ssh_host_key_failed"
    if any(text in value for text in ("permission denied (", "no supported authentication", "load key", "authentication failed")):
        return "ssh_authentication_failed"
    if any(text in value for text in ("address already in use", "cannot listen to port", "could not request local forwarding")):
        return "forwarding_port_in_use"
    if any(text in value for text in ("administratively prohibited", "remote port forwarding failed", "forwarding request failed")):
        return "forwarding_denied"
    if any(text in value for text in ("connection refused", "connection timed out", "network is unreachable", "could not resolve hostname", "no route to host")):
        return "ssh_endpoint_unreachable"
    return "ssh_disconnected"


class _BoundedErrors:
    def __init__(self, stream):
        self.stream = stream
        self.data = bytearray()
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        while True:
            # Buffered .read(n) can wait for a full block, hiding the final
            # authentication line while SSH waits for forwarding traffic.
            chunk = getattr(self.stream, 'read1', self.stream.read)(1024)
            if not chunk: return
            self.data.extend(chunk)
            if len(self.data) > 16384:
                del self.data[:-16384]

    def finish(self):
        self.thread.join(timeout=2)
        if not self.thread.is_alive(): self.stream.close()
        return classify_ssh_error(bytes(self.data))


def _port(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise BridgeError("configuration", name + " must be an integer from 1 to 65535")
    return value


def quote_ssh_option(value: str) -> str:
    """Quote one value for OpenSSH's own -o parser, independently of argv.

    Passing an argument list avoids shell parsing, but ssh still tokenizes -o
    values. Escape its double-quote and backslash syntax so a selected filename
    with spaces or quotes remains one filename, including Windows backslashes.
    """
    if (not isinstance(value, str) or not value or len(value) > 32768
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise BridgeError("configuration", "SSH option values must be nonempty and contain no control characters")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def configured_transports(cfg: dict) -> dict:
    transports = cfg.get("ssh_transports", {})
    if not isinstance(transports, dict):
        raise BridgeError("configuration", "ssh_transports must map peer IDs to transport settings")
    result = copy.deepcopy(transports)
    legacy = cfg.get("ssh_transport")
    if legacy:
        if not isinstance(legacy, dict):
            raise BridgeError("configuration", "ssh_transport must be an object")
        peer = legacy.get("peer_id")
        if not peer:
            peers = list(cfg.get("peers", {}))
            if len(peers) != 1:
                raise BridgeError("configuration", "Set peer_id on the legacy SSH transport before adding another peer")
            peer = peers[0]
        if peer in result:
            raise BridgeError("configuration", "A peer cannot have both legacy and named SSH transports")
        result[peer] = copy.deepcopy(legacy)
    for peer, settings in result.items():
        identifier(peer, "peer_id")
        if peer not in cfg.get("peers", {}):
            raise BridgeError("configuration", "An SSH transport must belong to a paired peer")
        if not isinstance(settings, dict):
            raise BridgeError("configuration", "Peer transport settings must be an object")
    return result


def transport_args(cfg: dict, peer_id: str) -> list[str]:
    transports = configured_transports(cfg)
    if peer_id not in transports:
        raise BridgeError("configuration", "No SSH transport is configured for this peer")
    t = transports[peer_id]
    ssh = t.get("ssh_exe") or shutil.which("ssh")
    if ssh and not Path(ssh).is_file(): ssh = shutil.which(str(ssh))
    if not ssh or not Path(ssh).is_file() or (os.name != "nt" and not os.access(ssh, os.X_OK)):
        raise BridgeError("configuration", "SSH executable not configured")
    for key in ("identity_file", "known_hosts_file"):
        value = t.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
            raise BridgeError("configuration", key + " must select an existing absolute local file")
    for key in ("ssh_port", "local_peer_port", "remote_bridge_port", "remote_peer_port"):
        _port(t.get(key), key)
    _port(cfg.get("listen_port"), "listen_port")
    for key in ("ssh_host", "username", "host_key_alias"):
        value = t.get(key)
        if not isinstance(value, str) or not value or value.startswith("-") or any(char.isspace() or char == "\0" for char in value):
            raise BridgeError("configuration", key + " must be a nonempty SSH name without whitespace")
    if t["local_peer_port"] == cfg["listen_port"]:
        raise BridgeError("configuration", "local_peer_port must differ from this daemon listen_port")
    if t["remote_peer_port"] == t["remote_bridge_port"]:
        raise BridgeError("configuration", "remote_peer_port must differ from the remote daemon port")
    return [str(ssh), "-F", "none", "-N", "-T", "-p", str(t["ssh_port"]), "-l", t["username"], "-i", t["identity_file"],
            "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
            "-o", "UserKnownHostsFile=" + quote_ssh_option(t["known_hosts_file"]), "-o", "HostKeyAlias=" + t["host_key_alias"],
            "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=10",
            "-L", f"127.0.0.1:{t['local_peer_port']}:127.0.0.1:{t['remote_bridge_port']}",
            "-R", f"127.0.0.1:{t['remote_peer_port']}:127.0.0.1:{cfg['listen_port']}", t["ssh_host"]]


def validate_transports(cfg: dict, *, check_files: bool = True) -> dict:
    transports = configured_transports(cfg)
    local_ports = set()
    for peer, transport in transports.items():
        if check_files:
            transport_args(cfg, peer)
        if transport.get("enabled"):
            port = _port(transport.get("local_peer_port"), "local_peer_port")
            if port in local_ports or port == cfg.get("listen_port"):
                raise BridgeError("configuration", "Enabled peers require distinct local forwarding ports")
            local_ports.add(port)
    return transports


def configure_transport(cfg: dict, peer_id: str, settings: dict) -> dict:
    identifier(peer_id, "peer_id")
    result = copy.deepcopy(cfg)
    # Resolve an old transport while its original single peer remains unambiguous.
    result["ssh_transports"] = configured_transports(cfg)
    result.pop("ssh_transport", None)
    result["ssh_transports"][peer_id] = copy.deepcopy(settings)
    validate_transports(result, check_files=False)
    transport_args(result, peer_id)
    return result


def _selected(cfg, peer_id):
    transports = configured_transports(cfg)
    if peer_id is not None:
        identifier(peer_id, "peer_id")
        if peer_id not in transports:
            raise BridgeError("configuration", "No SSH transport is configured for this peer")
        return [peer_id]
    return sorted(transports)


def transport_directory(cfg, peer_id):
    identifier(peer_id, "peer_id")
    return Path(cfg["state_dir"]) / "transports" / peer_id


def _read_record(path):
    from .cli import read
    try:
        return read(path)
    except (OSError, ValueError):
        return {}


def transport_status(path: str | Path, peer_id: str | None = None) -> dict:
    from .cli import read_config, record_status
    cfg = read_config(Path(path))
    transports = configured_transports(cfg)
    states = []
    for peer in _selected(cfg, peer_id):
        directory = transport_directory(cfg, peer)
        supervisor = record_status(directory / "supervisor.pid.json", directory / "supervisor.lock")
        child = record_status(directory / "ssh.pid.json")
        launching = record_status(directory / "launch.pid.json")
        current = _read_record(directory / "status.json")
        running = supervisor['running']
        states.append({"peer_id": peer, "enabled": bool(transports[peer].get("enabled")),
                       "paired": bool(cfg["peers"][peer].get("enabled")),
                       "stop_requested": (directory / "stop").exists(),
                       "supervisor_running": running, "ssh_running": child['identity_verified'],
                       "launch_pending": launching['running'] and not running,
                       "supervisor_identity_verified": supervisor['identity_verified'],
                       "ssh_identity_verified": child['identity_verified'],
                       "stale_process_record": supervisor['stale_pid_record'] or child['stale_pid_record'],
                       "state": current.get("state", "starting") if running or current.get("state") == "failed" else "stopped",
                       "updated_at": current.get("updated_at"), "last_error": current.get("last_error")})
    # Keep awareness of a pre-upgrade single supervisor. Never kill it using a
    # stale PID or start competing local forwards; owner stops it before upgrade.
    state = Path(cfg['state_dir'])
    legacy_running = record_status(state / 'transport-run.pid.json', state / 'transport.lock')['running']
    return {"transports": states, "legacy_supervisor_running": legacy_running,
            "note": "Process state alone does not verify forwarding; peer_status checks bridge authentication and reachability."}


def _launch(path, peer_id, directory):
    source = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy(); env["PYTHONPATH"] = source
    args = [sys.executable, "-m", "codex_bridge.cli", "--config", str(path), "transport-run", "--peer", peer_id]
    options = {"cwd": source, "env": env, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"):
            options["creationflags"] |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    else:
        options["start_new_session"] = True
    with (directory / "stdout.log").open("ab") as out, (directory / "stderr.log").open("ab") as err:
        return subprocess.Popen(args, stdout=out, stderr=err, **options)


def _spawn_ssh(args):
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
    if os.name == "nt": options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return processes.OwnedProcess(args, **options)


def _peer_processes(path, peer, directory):
    from . import autostart
    from .cli import record_status
    return {
        'supervisor': record_status(directory / 'supervisor.pid.json', directory / 'supervisor.lock'),
        'launcher': record_status(directory / 'launch.pid.json'),
        'runner': record_status(autostart.runner_record(path, 'transport', peer_id=peer),
                                autostart.runner_lock(path, 'transport', peer_id=peer)),
    }


def _wait_peer_stopped(path, peer, directory, timeout):
    deadline = time.monotonic() + timeout
    while True:
        states = _peer_processes(path, peer, directory)
        if not any(item['running'] for item in states.values()):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(.1)


def _request_peer_stop(path, peer, directory, deadline):
    from . import autostart
    from .cli import process_lock
    # An explicit stop must not land between a start's marker check and clear.
    # Both commands serialize their intent on this peer's launch decision lock.
    while True:
        try:
            with process_lock(directory / 'start.lock'):
                autostart.request_stop(path, 'transport', peer_id=peer)
            return
        except BridgeError as exc:
            if exc.code != 'already_running':
                raise
            if time.monotonic() >= deadline:
                raise BridgeError('transport_start_in_progress',
                    'This peer is completing a start request; retry transport-stop when it finishes', True) from exc
            time.sleep(.05)


def start_transports(path: str | Path, peer_id: str | None = None) -> dict:
    from . import autostart
    from .cli import read_config, process_lock, save
    path = Path(path)
    cfg = read_config(path)
    transports = validate_transports(cfg, check_files=False)
    if transport_status(path).get("legacy_supervisor_running"):
        raise BridgeError("legacy_transport_running", "Stop the old transport supervisor before starting the upgraded transport manager")
    results = []
    for peer in _selected(cfg, peer_id):
        try:
            entry = cfg.get('connections', {}).get(peer)
            if entry and entry.get('mode', 'on_demand') == 'on_demand':
                raise BridgeError('connection_manager_required', 'Use connect/connection_ensure for this negotiated peer; legacy transport start cannot compete with its manager')
            directory = transport_directory(cfg, peer); directory.mkdir(parents=True, exist_ok=True)
            if not transports[peer].get("enabled") or not cfg["peers"][peer].get("enabled"):
                results.append({"peer_id": peer, "started": False, "state": "disabled"}); continue
            transport_args(cfg, peer)
            # Serialize launch decisions as well as the supervisor lifetime. A
            # freshly spawned process is recorded with its OS creation identity
            # before another launcher can observe the startup window.
            with process_lock(directory / 'start.lock'):
                if (directory / 'stop').exists() and not _wait_peer_stopped(path, peer, directory, 15):
                    raise BridgeError('still_stopping', 'The selected peer transport or login runner is still stopping')
                states = _peer_processes(path, peer, directory)
                if any(item['running'] for item in states.values()):
                    results.append({'peer_id': peer, 'already_running': True,
                                    'ssh_connection_verified': False})
                    continue
                autostart.clear_stop(path, 'transport', peer_id=peer)
                # This is an explicit owner start, not a scheduler retry. The
                # compatibility supervisor has a finite journal as well.
                save(directory / 'legacy-budget.json', {'attempts': 0, 'state': 'authorized', 'authorized_at': now()})
                if autostart.enabled(path, 'transport', peer_id=peer):
                    result = autostart.start(path, 'transport', peer_id=peer)
                    results.append({'peer_id': peer, **result, 'started': False,
                                    'ssh_connection_verified': False})
                else:
                    proc = _launch(path, peer, directory)
                    save(directory / 'launch.pid.json', processes.record(proc.pid, created_at=now(), peer_id=peer))
                    results.append({'peer_id': peer, 'started': True, 'pid': proc.pid,
                                    'ssh_connection_verified': False})
        except (BridgeError, OSError) as exc:
            if peer_id is not None: raise
            error = exc.as_dict() if isinstance(exc, BridgeError) else {"code": "transport_start_failed", "message": "This peer transport could not start; inspect its local settings", "retryable": True}
            results.append({"peer_id": peer, "started": False, "state": "failed", "error": error})
    return {"ok": not any("error" in item for item in results), "transports": results}


def stop_transports(path: str | Path, peer_id: str | None = None, *, timeout: float = 10) -> dict:
    from .cli import read_config, record_status
    cfg = read_config(Path(path))
    directories = []
    errors = []
    deadline = time.monotonic() + timeout
    for peer in _selected(cfg, peer_id):
        directory = transport_directory(cfg, peer); directory.mkdir(parents=True, exist_ok=True)
        try:
            _request_peer_stop(path, peer, directory, deadline)
            directories.append((peer, directory))
        except (BridgeError, OSError) as exc:
            if peer_id is not None:
                raise
            error = exc.as_dict() if isinstance(exc, BridgeError) else {
                'code': 'transport_stop_failed', 'message': 'Could not persist this peer stop request', 'retryable': True}
            errors.append({'peer_id': peer, 'stop_requested': False, 'stopped': False, 'error': error})
    # Legacy supervisor knows its old stop marker. It can only be stopped by an
    # all-peers operation or the peer selected by its legacy configuration.
    legacy = cfg.get("ssh_transport")
    stop_legacy = peer_id is None or (legacy and peer_id in configured_transports(cfg) and peer_id not in cfg.get("ssh_transports", {}))
    if stop_legacy:
        (Path(cfg["state_dir"]) / "transport.stop").touch()
    while time.monotonic() < deadline:
        active = any(any(item['running'] for item in _peer_processes(path, peer, directory).values())
                     for peer, directory in directories)
        old_state = Path(cfg['state_dir'])
        if stop_legacy and record_status(old_state / 'transport-run.pid.json', old_state / 'transport.lock')['running']:
            active = True
        if not active:
            break
        time.sleep(.1)
    state = Path(cfg['state_dir'])
    return {'ok': not errors, 'legacy_stopped': not record_status(state / 'transport-run.pid.json', state / 'transport.lock')['running'],
            'transports': [{'peer_id': peer, 'stop_requested': True,
                           'stopped': not any(item['running'] for item in _peer_processes(path, peer, directory).values())}
                          for peer, directory in directories] + errors}


def supervise_transport(path: str | Path, peer_id: str) -> dict:
    from .cli import read_config, process_lock, save
    path = Path(path)
    cfg = read_config(path)
    _selected(cfg, peer_id)
    entry = cfg.get('connections', {}).get(peer_id)
    if entry and entry.get('mode', 'on_demand') == 'on_demand':
        raise BridgeError('connection_manager_required', 'The daemon connection manager owns this peer; legacy supervisors cannot start it')
    directory = transport_directory(cfg, peer_id); directory.mkdir(parents=True, exist_ok=True)
    stop = directory / "stop"
    with process_lock(directory / "supervisor.lock"):
        save(directory / 'supervisor.pid.json', processes.record(os.getpid(), created_at=now(), peer_id=peer_id))
        budget_path = directory / 'legacy-budget.json'
        budget = _read_record(budget_path)
        if budget.get('state') in ('attempting', 'exhausted', 'suspended'):
            budget['state'] = 'suspended' if budget.get('state') == 'attempting' else budget['state']
            save(budget_path, budget)
            save(directory / 'status.json', {'state': 'failed', 'updated_at': now(),
                 'last_error': {'code': 'explicit_retry_required', 'message': 'Legacy attempt budget is exhausted or interrupted; an explicit owner transport-start is required'}})
            (directory / 'supervisor.pid.json').unlink(missing_ok=True)
            return {'peer_id': peer_id, 'stopped': True, 'retry_required': True}
        budget = {'attempts': 0, 'state': 'attempting', 'started_at': now()}
        deadline = time.monotonic() + 45
        save(budget_path, budget)
        child = None
        owner = None
        errors = None
        def status(state, error=None):
            save(directory / "status.json", {"state": state, "updated_at": now(), "last_error": error})
        try:
            while not stop.exists():
                if budget['attempts'] >= 2 or time.monotonic() >= deadline:
                    budget['state'] = 'exhausted'; save(budget_path, budget)
                    status('failed', {'code': 'explicit_retry_required', 'message': 'The bounded legacy SSH attempts ended; request an explicit retry'})
                    break
                cfg = read_config(path)
                transports = configured_transports(cfg)
                t = transports.get(peer_id)
                if not t or not t.get("enabled") or not cfg.get("peers", {}).get(peer_id, {}).get("enabled"):
                    break
                args = transport_args(cfg, peer_id)
                status("connecting")
                budget['attempts'] += 1
                save(budget_path, budget)  # Charge durably before creating SSH.
                owner = _spawn_ssh(args)
                child = owner.process
                errors = _BoundedErrors(child.stderr)
                save(directory / 'ssh.pid.json', processes.record(child.pid, created_at=now(), peer_id=peer_id))
                status("forwarding_unverified")
                while child.poll() is None and not stop.exists():
                    latest = read_config(path)
                    if not latest.get("peers", {}).get(peer_id, {}).get("enabled") or configured_transports(latest).get(peer_id) != t:
                        break
                    time.sleep(.25)
                owner.close()
                owner = None
                error_code = errors.finish()
                errors = None
                if stop.exists(): break
                latest = read_config(path)
                if not latest.get('peers', {}).get(peer_id, {}).get('enabled') or configured_transports(latest).get(peer_id) != t:
                    break
                if error_code in ('ssh_host_key_failed', 'ssh_authentication_failed', 'forwarding_port_in_use', 'forwarding_denied'):
                    budget['state'] = 'exhausted'; save(budget_path, budget)
                    status('failed', {'code': error_code})
                    break
                status("retrying", {"code": error_code, "exit_code": child.returncode})
                retry_at = min(deadline, time.monotonic() + 5)
                while time.monotonic() < retry_at and not stop.exists(): time.sleep(.1)
        except (OSError, BridgeError) as exc:
            status("failed", {"code": getattr(exc, "code", "ssh_start_failed"), "message": "SSH transport could not continue; check local settings and forwarding permissions"})
            raise
        finally:
            if owner is not None:
                owner.close()
            if errors is not None: errors.finish()
            (directory / "ssh.pid.json").unlink(missing_ok=True)
            (directory / "supervisor.pid.json").unlink(missing_ok=True)
            if _read_record(directory / "status.json").get("state") != "failed": status("stopped")
    return {"peer_id": peer_id, "stopped": True}
