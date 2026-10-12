"""OS-neutral locks and read-only process checks (including Windows)."""

from pathlib import Path

import psutil
from filelock import FileLock, Timeout


def acquire_lock(path: Path) -> FileLock:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path), timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise BlockingIOError("Another process holds the lock") from exc
    return lock


def process_running(pid: int, created_at: float | None = None) -> bool:
    if pid <= 0:
        return False
    try:
        process = psutil.Process(pid)
        if created_at is not None and abs(process.create_time() - created_at) > 1:
            return False  # Do not mistake a reused PID for our pipeline.
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
