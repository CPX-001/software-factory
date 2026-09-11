"""Controller-owned sandbox launcher, process identity and bounded command capture."""
import json
import fcntl
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from .registry import FactoryError


def process_identity(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'start': stat[19], 'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, IndexError):
        return None


def is_alive(identity):
    return bool(identity and process_identity(identity['pid']) == identity)


class LinuxSandbox:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lifetime = 3600

    def command(self, argv, mounts=(), *, mode='test', lifetime=None):
        if sys.platform != 'linux' or not shutil.which('unshare'):
            raise FactoryError('isolation_unavailable', 'Execution requires Linux user/mount/PID/network namespaces and seccomp')
        place = Path(tempfile.mkdtemp(prefix='sandbox-', dir=self.directory))
        (place / 'root').mkdir()
        spec = {'root': str(place / 'root'), 'mounts': list(mounts), 'mode': mode, 'argv': argv,
                'lease': str(self.directory / 'process.lock'),
                'lifetime': self.lifetime if lifetime is None else min(lifetime, self.lifetime),
                'pause_file': str(self.directory / 'pause')}
        location = place / 'launch.json'
        location.write_text(json.dumps(spec))
        cmd = ['/usr/bin/unshare', '--user', '--map-root-user', '--mount', '--pid', '--fork', '--kill-child=KILL']
        if mode == 'test':
            cmd.append('--net')
        child = cmd + [sys.executable, str(Path(__file__).with_name('sandbox_entry.py')), str(location)]
        return [sys.executable, str(Path(__file__).with_name('sandbox_supervisor.py')), str(location), json.dumps(child)]

    def live(self):
        with (self.directory / 'process.lock').open('a') as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False

    def run(self, argv, mounts=(), *, timeout=30, should_stop=lambda: False, on_process=lambda x: None):
        command = self.command(argv, mounts, lifetime=timeout)
        start = time.monotonic()
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        identity = process_identity(process.pid)
        on_process(identity)
        log = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        reason = None
        try:
            while selector.get_map():
                if should_stop():
                    reason = 'interrupted'
                elif time.monotonic() - start >= timeout:
                    reason = 'timeout'
                if reason:
                    if is_alive(identity):
                        os.killpg(process.pid, signal.SIGKILL)
                    break
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        log.extend(chunk)
                        if len(log) > 24000:
                            del log[:len(log) - 24000]
            code = process.wait(timeout=5)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            selector.close()
            process.stdout.close()
        output = log.decode('utf-8', errors='replace')
        status = ('NOT_RUN' if reason or code < 0 or code in (124, 125, 126, 127) else 'PASS' if code == 0 else 'FAIL')
        if 'FACTORY_SANDBOX_UNAVAILABLE:' in output:
            status, reason = 'NOT_RUN', 'isolation_unavailable'
        return {'status': status, 'reason': reason, 'command': argv, 'environment': 'linux-namespaces-seccomp-v1',
                'exit_code': code, 'duration_seconds': round(time.monotonic() - start, 4),
                'log': output, 'log_limit_bytes': 24000, 'process': identity}

    def probe(self):
        code = '''import os, socket, subprocess
assert not os.path.exists('/factory-controller-sentinel')
assert os.listdir('/workspace') == []
try:
    socket.create_connection(('1.1.1.1', 443), timeout=.1)
except OSError:
    pass
else:
    raise AssertionError('network escaped')
assert subprocess.run(['/usr/bin/unshare', '--mount', 'true'], capture_output=True).returncode != 0
print('isolation-ok')
'''
        result = self.run(['/usr/bin/python3', '-I', '-c', code], timeout=10)
        if result['status'] != 'PASS' or 'isolation-ok' not in result['log']:
            raise FactoryError('isolation_unavailable', 'Sandbox capability probe failed', details={'probe': result})
        return {'available': True, 'profile': 'linux-namespaces-seccomp-v1', 'tested_at': time.time()}
