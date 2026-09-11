"""Private non-LLM process guardian; the lease is never inherited by product code."""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    spec = json.loads(Path(sys.argv[1]).read_text())
    command = json.loads(sys.argv[2])
    with open(spec['lease'], 'a') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def child_setup():
            ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0)
            if os.getppid() == 1:
                os._exit(125)
        process = subprocess.Popen(command, preexec_fn=child_setup, close_fds=True)
        def stop(*_):
            process.kill()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        deadline = time.monotonic() + spec.get('lifetime', 3600)
        pause_seen = None
        while process.poll() is None:
            paused = Path(spec['pause_file']).exists()
            if paused and pause_seen is None:
                pause_seen = time.monotonic()
            if not paused:
                pause_seen = None
            # Give the SDK controller its native interrupt grace period first. This
            # guardian also works if the controller crashed or is stuck initializing.
            if time.monotonic() >= deadline or (pause_seen is not None and time.monotonic() - pause_seen >= 3):
                process.kill()
                break
            time.sleep(.1)
        code = process.wait()
        if spec['mode'] == 'runtime':
            for source, destination, _ in spec['mounts']:
                if destination == '/home':
                    (Path(source) / 'auth.json').unlink(missing_ok=True)
        return code


if __name__ == '__main__':
    sys.exit(main())
