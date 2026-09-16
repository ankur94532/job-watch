"""Single-run lock that can never go stale.

flock(2) is held by the open file descriptor, and the kernel drops it when the
process exits for any reason — killed, crashed, or frozen by sleep and then
reaped. There is no PID file to clean up. The PID written inside is only for
humans reading the file.
"""

import fcntl
import os


class AlreadyRunning(Exception):
    pass


class RunLock:
    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = ""
            try:
                holder = os.pread(self.fd, 32, 0).decode().strip()
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None
            raise AlreadyRunning(f"another run holds {self.path}" + (f" (pid {holder})" if holder else ""))
        os.ftruncate(self.fd, 0)
        os.pwrite(self.fd, str(os.getpid()).encode(), 0)
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
