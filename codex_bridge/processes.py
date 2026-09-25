"""Local process identity and exact child ownership; no process-name kills."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import subprocess
import sys
import time


def _mac_identity(pid):
    # Binding to Apple's public proc_bsdinfo/proc_pidpath API. Creation time is
    # microsecond precision; a process name or PID alone is never an identity.
    import ctypes
    class BSDInfo(ctypes.Structure):
        _fields_ = [('head', ctypes.c_uint32 * 12), ('command', ctypes.c_char * 16),
                    ('name', ctypes.c_char * 32), ('tail', ctypes.c_uint32 * 6),
                    ('start_seconds', ctypes.c_uint64), ('start_microseconds', ctypes.c_uint64)]
    try:
        lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidinfo.restype = ctypes.c_int
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        lib.proc_pidpath.restype = ctypes.c_int
        first, second = BSDInfo(), BSDInfo()
        if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(first), ctypes.sizeof(first)) != ctypes.sizeof(first): return None
        if first.head[3] != pid or first.head[1] == 5: return None
        executable = ctypes.create_string_buffer(4096)
        if lib.proc_pidpath(pid, executable, len(executable)) <= 0: return None
        if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(second), ctypes.sizeof(second)) != ctypes.sizeof(second): return None
        if (first.start_seconds, first.start_microseconds) != (second.start_seconds, second.start_microseconds) or second.head[1] == 5: return None
        return {'creation_time': f'{first.start_seconds}:{first.start_microseconds}',
                'executable': os.fsdecode(executable.value)}
    except (OSError, AttributeError):
        return None


def identity(pid):
    """Return an OS creation identity, or None; a reused PID is never enough."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            creation, exit_time, user, system = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(user), ctypes.byref(system)):
                return None
            size = wintypes.DWORD(32768)
            executable = ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(handle, 0, executable, ctypes.byref(size)):
                return None
            return {'creation_time': str((creation.dwHighDateTime << 32) | creation.dwLowDateTime),
                    'executable': executable.value.casefold()}
        finally:
            kernel.CloseHandle(handle)
    if sys.platform == 'darwin':
        return _mac_identity(pid)
    try:
        # Linux tests/deployments retain the same creation-identity guarantee.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return {'creation_time': boot + ':' + fields[19],
                'executable': str(Path(f'/proc/{pid}/exe').resolve(strict=True))}
    except (OSError, IndexError):
        return None


def record(pid, **metadata):
    return {'pid': pid, 'process_identity': identity(pid), **metadata}


def matches(record):
    expected = record.get('process_identity') if isinstance(record, dict) else None
    return bool(expected and expected == identity(record.get('pid')))


def login_identity():
    """Windows authentication LUID changes at sign-out, but not screen lock."""
    if os.name != 'nt':
        if sys.platform == 'darwin':
            boot = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.boottime'], capture_output=True, text=True, check=True, timeout=5).stdout.strip()
            return str(os.getuid()) + ':' + boot
        return str(os.getuid()) + ':' + Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    class Luid(ctypes.Structure):
        _fields_ = [('low', wintypes.DWORD), ('high', wintypes.LONG)]
    class Statistics(ctypes.Structure):
        _fields_ = [('token_id', Luid), ('authentication_id', Luid), ('expiration', ctypes.c_longlong),
                   ('token_type', wintypes.DWORD), ('impersonation', wintypes.DWORD), ('dynamic_charged', wintypes.DWORD),
                   ('dynamic_available', wintypes.DWORD), ('group_count', wintypes.DWORD), ('privilege_count', wintypes.DWORD), ('modified_id', Luid)]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), 'Cannot inspect the owner logon token')
    try:
        statistics, size = Statistics(), wintypes.DWORD()
        if not advapi.GetTokenInformation(token, 10, ctypes.byref(statistics), ctypes.sizeof(statistics), ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), 'Cannot inspect the owner logon identity')
        auth = statistics.authentication_id
        return f'{auth.high & 0xffffffff:08x}:{auth.low:08x}'
    finally:
        kernel.CloseHandle(token)


def interactive_session():
    """SSH service commands run in Session 0, unlike the signed-in desktop."""
    if os.name != 'nt':
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        return False
    return session.value != 0


def _waitable_exit(pid):
    """Observe a child without reaping: its PID remains reserved until cleanup."""
    if hasattr(os, 'waitid'):
        result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is None: return None
        return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status
    if sys.platform == 'darwin':
        # Python <3.13 did not expose macOS waitid. The public libc API does.
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.waitid.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
        info = (ctypes.c_long * 128)()  # Aligned, larger than Darwin siginfo_t.
        if libc.waitid(1, pid, ctypes.byref(info), 4 | 1 | 32) != 0:
            error = ctypes.get_errno()
            if error == 10: raise ChildProcessError(error, 'Child ownership was lost')
            raise OSError(error, 'Cannot inspect owned child exit')
        fields = ctypes.cast(ctypes.byref(info), ctypes.POINTER(ctypes.c_int))
        if fields[3] == 0: return None
        return fields[5] if fields[2] == 1 else -fields[5]
    raise OSError('This platform cannot retain child ownership during group cleanup')


class _RetainedProcess:
    """Popen facade retaining the child PID until its process group is cleaned."""
    def __init__(self, child):
        self._child = child
        self._closed = False
        self._owned = True
        self._code = None

    def __getattr__(self, name): return getattr(self._child, name)

    @property
    def returncode(self): return self.poll()

    def poll(self):
        if self._closed: return self._code
        try: self._code = _waitable_exit(self.pid)
        except ChildProcessError:
            self._owned = False
            raise
        return self._code

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic()+timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(.01)
        return self._code

    def terminate(self):
        import signal
        if self._owned and not self._closed: os.kill(self.pid, signal.SIGTERM)

    def kill(self):
        import signal
        if self._owned and not self._closed: os.kill(self.pid, signal.SIGKILL)

    def close(self):
        if self._closed: return
        import signal
        # Even an exited leader is still our waitable child. Its reserved PID
        # cannot be reused for another group until after this group cleanup.
        with contextlib.suppress(ChildProcessError): self.poll()
        if self._owned:
            with contextlib.suppress(ProcessLookupError): os.killpg(self.pid, signal.SIGKILL)
        self._code = self._child.wait(timeout=5)
        self._closed = True


class OwnedProcess:
    """Assign a suspended Windows child to a kill-on-close job before it runs."""
    def __init__(self, argv, **options):
        self.process = None
        self.job = None
        try:
            if os.name == 'nt':
                from .local_actions import _WindowsJob
                options['creationflags'] = options.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW | 4
                self.process = subprocess.Popen(argv, **options)
                self.job = _WindowsJob(self.process.pid)
                self.job.resume(self.process.pid)
            else:
                options['start_new_session'] = True
                self.process = _RetainedProcess(subprocess.Popen(argv, **options))
        except BaseException:
            self.close()
            raise

    def close(self):
        if isinstance(self.process, _RetainedProcess):
            self.process.close()
            return
        if self.job:
            self.job.close()
            self.job = None
        if self.process and self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                if os.name == 'nt':
                    self.process.kill()  # Popen retains its exact process handle.
                else:
                    import signal
                    os.killpg(self.process.pid, signal.SIGKILL)
            # Termination is asynchronous. Keep the exact Popen handle and
            # propagate an unconfirmed exit so callers cannot retire ownership
            # or report a released forwarding port while this child survives.
            self.process.wait(timeout=5)

    def __enter__(self):
        return self.process

    def __exit__(self, *args):
        self.close()
