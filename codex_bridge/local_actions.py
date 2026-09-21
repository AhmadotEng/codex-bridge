"""Owner-configured, fixed project actions; never a peer-selected shell.

The normal Codex worker sandbox is unchanged. This separate local capability
executes only the reviewed argv and guarded files selected in owner config.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .core import BridgeError, canonical, digest, identifier, now


def _plain_path(value, *, directory=False):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise BridgeError('action_configuration', 'Action paths must be absolute')
    path = Path(value)
    for part in [path, *path.parents]:
        if part.is_symlink() or (part.exists() and getattr(part.stat(), 'st_file_attributes', 0) & 1024):
            raise BridgeError('action_configuration', 'Action paths cannot contain links or reparse points')
    if not (path.is_dir() if directory else path.is_file()):
        raise BridgeError('action_configuration', 'An action path is unavailable')
    return path.resolve()


def registry(project):
    actions = project.get('local_actions', {})
    if not isinstance(actions, dict) or len(actions) > 16:
        raise BridgeError('action_configuration', 'local_actions must be a dictionary of at most 16 fixed actions')
    for action_id, action in actions.items():
        identifier(action_id, 'action_id')
        if not isinstance(action, dict) or set(action) - {'title', 'argv', 'cwd', 'guard_files', 'timeout_seconds', 'max_output_bytes'}:
            raise BridgeError('action_configuration', 'Unrecognized fixed action configuration')
        if not isinstance(action.get('title'), str) or not 1 <= len(action['title']) <= 400:
            raise BridgeError('action_configuration', 'A short action title is required')
        argv = action.get('argv')
        if not isinstance(argv, list) or not 1 <= len(argv) <= 32 or any(not isinstance(a, str) or not a or '\0' in a or len(a) > 8192 for a in argv):
            raise BridgeError('action_configuration', 'argv must contain fixed nonempty strings')
        _plain_path(argv[0])
        _plain_path(action.get('cwd'), directory=True)
        if not isinstance(action.get('timeout_seconds', 120), (float, int)) or not 1 <= action.get('timeout_seconds', 120) <= 600:
            raise BridgeError('action_configuration', 'Action timeout must be 1 to 600 seconds')
        if not isinstance(action.get('max_output_bytes', 16000), int) or not 128 <= action.get('max_output_bytes', 16000) <= 64000:
            raise BridgeError('action_configuration', 'Action output limit must be 128 to 64000 bytes')
        guards = action.get('guard_files')
        if not isinstance(guards, list) or not 1 <= len(guards) <= 32:
            raise BridgeError('action_configuration', 'At least one guarded action file is required')
        for guard in guards:
            if not isinstance(guard, dict) or set(guard) != {'path', 'sha256'} or not re.fullmatch('[a-fA-F0-9]{64}', str(guard.get('sha256', ''))):
                raise BridgeError('action_configuration', 'Each guard requires an absolute path and SHA256')
            path = _plain_path(guard['path'])
            if project.get('policy') == 'workspace-write' and path.is_relative_to(Path(project['workspace']).resolve()):
                raise BridgeError('action_configuration', 'Guarded executables and scripts must be outside the worker writable workspace')
    return actions


def descriptions(project):
    return [{'action_id': key, 'title': value['title']} for key, value in registry(project).items()]


def _safe_output(value, depth=0):
    if depth > 8:
        return '[depth limit]'
    if isinstance(value, dict):
        return {str(k)[:200]: '[redacted]' if re.search(r'(?i)(password|secret|private.?key|authorization|access.?token|refresh.?token|cookie)', str(k)) else _safe_output(v, depth+1) for k, v in list(value.items())[:200]}
    if isinstance(value, list):
        return [_safe_output(v, depth+1) for v in value[:200]]
    if isinstance(value, str):
        return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)[:8000]
    return value


class _WindowsJob:
    """Kill only the exact owned subprocess and descendants when closing."""
    def __init__(self, pid):
        import ctypes
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        k = self.kernel
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        class Basic(ctypes.Structure):
            _fields_ = [('per_process', ctypes.c_longlong), ('per_job', ctypes.c_longlong), ('flags', wintypes.DWORD), ('minimum', ctypes.c_size_t), ('maximum', ctypes.c_size_t), ('active', wintypes.DWORD), ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        self.handle = k.CreateJobObjectW(None, None)
        info = Extended(); info.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE
        process = k.OpenProcess(0x0001 | 0x0100, False, pid)
        try:
            if not self.handle or not process or not k.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not k.AssignProcessToJobObject(self.handle, process):
                self.close()
                raise BridgeError('action_process_scope', 'Cannot establish action process ownership; action stopped')
        finally:
            if process: k.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def resume(self, pid):
        """Resume the newly created primary thread only after job assignment."""
        import ctypes
        from ctypes import wintypes
        k = self.kernel
        class ThreadEntry(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('thread_id', wintypes.DWORD),
                ('owner_pid', wintypes.DWORD), ('base_priority', wintypes.LONG), ('delta_priority', wintypes.LONG), ('flags', wintypes.DWORD)]
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        k.Thread32First.restype = wintypes.BOOL
        k.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        k.Thread32Next.restype = wintypes.BOOL
        k.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenThread.restype = wintypes.HANDLE
        k.ResumeThread.argtypes = [wintypes.HANDLE]
        k.ResumeThread.restype = wintypes.DWORD
        snapshot = k.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise BridgeError('action_process_scope', 'Cannot resume the owned action process')
        resumed = False
        try:
            entry = ThreadEntry(); entry.size = ctypes.sizeof(entry)
            more = k.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.owner_pid == pid:
                    thread = k.OpenThread(2, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise BridgeError('action_process_scope', 'Cannot resume the owned action process')
                    try:
                        previous = k.ResumeThread(thread)
                        if previous == 0xffffffff:
                            raise BridgeError('action_process_scope', 'Cannot resume the owned action process')
                        resumed = resumed or previous > 0
                    finally:
                        k.CloseHandle(thread)
                more = k.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            k.CloseHandle(snapshot)
        if not resumed:
            raise BridgeError('action_process_scope', 'Owned action had no suspended primary thread')


class LocalActions:
    def __init__(self, bridge):
        self.bridge = bridge
        for item in bridge.store.all('local_actions'):
            if item['status'] in ('starting', 'running'):
                item.update(status='uncertain', updated_at=now(), error={'code': 'process_interrupted', 'message': 'Action may have run. Inspect its effects before assigning a new request ID.'})
                bridge.store.put('local_actions', item['request_id'], item)

    async def execute(self, *, session_id, task_id, expected_registry, action_id, request_id):
        identifier(action_id, 'action_id'); identifier(request_id, 'request_id')
        session = self.bridge.session(session_id, operation='tasks')
        project = self.bridge.project(session['project_id'], session['peer_id'])
        actions = registry(project)
        if digest(actions) != expected_registry or action_id not in actions:
            raise BridgeError('action_scope_changed', 'The owner action registry changed or this action is unavailable')
        task = self.bridge.store.get('incoming', task_id)
        if not task or task['session_id'] != session_id or task['status'] not in ('starting', 'running'):
            raise BridgeError('action_cancelled', 'The bridge task is no longer active')
        intent = digest({'session_id': session_id, 'action_id': action_id, 'registry': expected_registry})
        existing = self.bridge.store.get('local_actions', request_id)
        if existing:
            if existing['intent_digest'] != intent:
                raise BridgeError('duplicate_conflict', 'Action request ID already identifies another action or session')
            return {**existing, 'deduplicated': True}
        action = actions[action_id]
        # Hash validation and durable claim have no intervening await: another
        # bridge callback cannot race this claim. The owner protects scripts
        # outside the worker write root against modification after validation.
        for guard in action['guard_files']:
            h = hashlib.sha256()
            with _plain_path(guard['path']).open('rb') as source:
                while chunk := source.read(1024*1024): h.update(chunk)
            if h.hexdigest() != guard['sha256'].lower():
                raise BridgeError('action_guard_mismatch', 'A guarded action file changed; action was not executed')
        result = {'request_id': request_id, 'action_id': action_id, 'session_id': session_id,
                  'task_id': task_id, 'intent_digest': intent, 'status': 'starting', 'created_at': now(), 'updated_at': now()}
        self.bridge.store.put('local_actions', request_id, result)
        process = None; job = None
        async def stop():
            if job: job.close()
            if process and process.returncode is None:
                if os.name != 'nt':
                    import signal
                    with contextlib.suppress(ProcessLookupError): os.killpg(process.pid, signal.SIGKILL)
                else:
                    with contextlib.suppress(ProcessLookupError): process.kill()
                with contextlib.suppress(asyncio.TimeoutError): await asyncio.wait_for(process.wait(), 3)
            if process:
                transport = getattr(process, '_transport', None)
                if transport: transport.close()
        try:
            # CREATE_SUSPENDED prevents descendants escaping before the Job
            # owns the process. No script code runs until job.resume().
            options = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004} if os.name == 'nt' else {'start_new_session': True}
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(*action['argv'], cwd=action['cwd'], stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, **options))
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # Keep the spawn future owned even if cancellation arrives
                # while asyncio is connecting its pipes. The Windows child
                # remains suspended and cannot launch untracked children.
                def dispose(future):
                    if future.cancelled() or future.exception(): return
                    child = future.result()
                    if child.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            if os.name == 'nt': child.kill()
                            else:
                                import signal
                                os.killpg(child.pid, signal.SIGKILL)
                    transport = getattr(child, '_transport', None)
                    if transport: transport.close()
                spawn.add_done_callback(dispose)
                raise
            if os.name == 'nt':
                job = _WindowsJob(process.pid)
                job.resume(process.pid)
            result.update(status='running', updated_at=now())
            self.bridge.store.put('local_actions', request_id, result)
            async def collect():
                output = bytearray()
                while chunk := await process.stdout.read(4096):
                    output.extend(chunk)
                    if len(output) > action.get('max_output_bytes', 16000):
                        raise BridgeError('action_output_limit', 'Action output exceeded its configured limit')
                await process.wait()
                return bytes(output)
            raw = await asyncio.wait_for(collect(), action.get('timeout_seconds', 120))
            try:
                output = json.loads(raw.decode('utf-8-sig'))
                if not isinstance(output, (dict, list)): raise ValueError()
            except (ValueError, UnicodeError):
                raise BridgeError('action_output_invalid', 'Action must emit only a JSON object or array; raw output was withheld')
            result.update(status='completed' if process.returncode == 0 else 'failed', exit_code=process.returncode, output=_safe_output(output))
        except asyncio.CancelledError:
            result.update(status='cancelled', error={'code': 'action_cancelled', 'message': 'Only this owned action process tree was stopped'})
        except asyncio.TimeoutError:
            result.update(status='failed', error={'code': 'action_timeout', 'message': 'The fixed action exceeded its configured timeout'})
        except Exception as exc:
            error = exc.as_dict() if isinstance(exc, BridgeError) else {'code': 'action_execution_failed', 'message': 'The owner-configured action failed; inspect local configuration'}
            result.update(status='failed', error=error)
        finally:
            await stop()
            if process: result.setdefault('exit_code', process.returncode)
            result['updated_at'] = now()
            self.bridge.store.put('local_actions', request_id, result)
        return result
