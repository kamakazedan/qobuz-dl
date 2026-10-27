"""
Download queue.

Start `qobuz-dl dl ...` or `qobuz-dl lucky ...` while another one is downloading
and it's added to a queue instead of running alongside it. The qobuz-dl that's
already running works through the queue, oldest first, once its own task is done.

Each entry is the command line as typed plus the folder it was typed in, and it
runs as its own qobuz-dl process, so its flags (-q, -d, --no-db, ...) and any
relative paths apply exactly as typed. An entry is only removed after it has run,
so closing qobuz-dl or a crash loses nothing: the next download, or
`qobuz-dl queue --run`, picks up what's left.

The queue is queue.jsonl in the config folder. Two OS file locks coordinate the
processes, and the OS releases them if a process dies, so they can't go stale:
worker.lock is held by the qobuz-dl that's downloading for as long as it runs;
queue.lock is held briefly around every read or change of the queue file.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager

from qobuz_dl.color import CYAN, GREEN, YELLOW, OFF

# Commands that wait their turn. Interactive mode needs you at the keyboard, so
# it never queues and isn't held up by the queue.
QUEUED_COMMANDS = ("dl", "lucky")
_CHILD_ENV = "QOBUZ_DL_QUEUE_CHILD"


def is_queued_run() -> bool:
    """True inside a qobuz-dl started from the queue: it just runs."""
    return os.environ.get(_CHILD_ENV) == "1"


class _FileLock:
    """An exclusive OS lock on a file, released automatically if the process dies."""

    def __init__(self, path):
        self.path = path
        self._file = None

    def acquire(self, blocking=True) -> bool:
        lock_file = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(lock_file.fileno(), mode, 1)
            else:
                import fcntl

                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(lock_file.fileno(), flags)
        except OSError:
            lock_file.close()
            if blocking:
                raise
            return False
        self._file = lock_file
        return True

    def release(self):
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def describe(entry: dict) -> str:
    """The queued command as you'd type it."""
    return subprocess.list2cmdline(entry.get("argv", []))


def _command(entry: dict) -> list:
    if getattr(sys, "frozen", False):  # standalone build: the exe is qobuz-dl itself
        return [sys.executable, *entry["argv"]]
    return [sys.executable, "-m", "qobuz_dl", *entry["argv"]]


def _run(entry: dict) -> int:
    """Run a queued command in its own qobuz-dl process, in the folder it was
    typed in, with its output in this window."""
    cwd = entry.get("cwd")
    if not (cwd and os.path.isdir(cwd)):
        cwd = None
    env = dict(os.environ, **{_CHILD_ENV: "1"})
    return subprocess.run(_command(entry), cwd=cwd, env=env).returncode


class DownloadQueue:
    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "queue.jsonl")
        self._queue_lock = _FileLock(os.path.join(folder, "queue.lock"))
        self._worker_lock = _FileLock(os.path.join(folder, "worker.lock"))
        self.is_worker = False

    # --- the queue file (only touched while holding the queue lock) ---------
    @contextmanager
    def _locked(self):
        self._queue_lock.acquire()
        try:
            yield
        finally:
            self._queue_lock.release()

    def _read(self) -> list:
        entries = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        pass  # skip a damaged line rather than lose the rest
        except FileNotFoundError:
            pass
        return entries

    def _write(self, entries: list):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        for attempt in range(5):  # a virus scanner can hold the file for a moment
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2)

    def entries(self) -> list:
        with self._locked():
            return self._read()

    def clear(self) -> int:
        with self._locked():
            count = len(self._read())
            self._write([])
        return count

    # --- who's downloading ----------------------------------------------------
    def enqueue_if_busy(self, argv, cwd) -> int:
        """If nothing else is downloading, make this process the worker and
        return 0. Otherwise add the command to the queue for the qobuz-dl that is,
        and return its position."""
        with self._locked():
            if self._worker_lock.acquire(blocking=False):
                self.is_worker = True
                return 0
            entry = {
                "id": uuid.uuid4().hex,
                "argv": list(argv),
                "cwd": cwd,
                "added": time.strftime("%Y-%m-%d %H:%M"),
            }
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            return len(self._read())

    def claim(self) -> bool:
        """Become the worker if nothing else is downloading."""
        with self._locked():
            self.is_worker = self._worker_lock.acquire(blocking=False)
        return self.is_worker

    def is_running(self) -> bool:
        """Whether a qobuz-dl is downloading (and so will work through the queue)."""
        if self.is_worker:
            return True
        probe = _FileLock(self._worker_lock.path)
        if probe.acquire(blocking=False):
            probe.release()
            return False
        return True

    def release(self):
        if self.is_worker:
            self._worker_lock.release()
            self.is_worker = False

    def run_all(self, runner=_run):
        """Run queued commands, oldest first, until the queue is empty, then stop
        being the worker. Interrupting it (Ctrl+C) leaves the rest queued."""
        done = 0
        try:
            while True:
                with self._locked():
                    queued = self._read()
                    if not queued:
                        # Stop being the worker while still holding the queue lock,
                        # so anything queued from here on starts a run of its own
                        # instead of waiting on a worker that's about to exit.
                        self.release()
                        break
                entry, more = queued[0], len(queued) - 1
                print(
                    f"\n{CYAN}[queue] Starting: qobuz-dl {describe(entry)}"
                    + (f" ({more} more queued)" if more else "")
                    + OFF
                )
                code = runner(entry)
                if code:
                    print(f"{YELLOW}[queue] That one ended with an error ({code}){OFF}")
                with self._locked():
                    rest = [e for e in self._read() if e.get("id") != entry.get("id")]
                    self._write(rest)
                done += 1
        finally:
            if self.is_worker:  # interrupted part-way
                self.release()
                left = len(self.entries())
                if left:
                    print(
                        f"\n{YELLOW}[queue] Stopped with {left} item(s) still queued. "
                        "Run 'qobuz-dl queue --run' to carry on, or "
                        f"'qobuz-dl queue --clear' to drop them.{OFF}"
                    )
        if done:
            print(f"\n{GREEN}[queue] Queue finished ({done} item(s) run).{OFF}")


def queue_command(queue: DownloadQueue, run=False, clear=False):
    """`qobuz-dl queue`: show the queue, or --clear it, or --run it now."""
    if clear:
        print(f"{GREEN}[queue] Removed {queue.clear()} item(s).{OFF}")
        return
    entries = queue.entries()
    if not entries:
        print("[queue] Nothing queued.")
        return
    if run:
        if queue.claim():
            queue.run_all()
        else:
            print(
                f"{YELLOW}[queue] A download is already running; it will work "
                f"through the queue when it finishes.{OFF}"
            )
        return
    print(f"[queue] {len(entries)} item(s) queued:")
    for number, entry in enumerate(entries, 1):
        print(f"  {number}. qobuz-dl {describe(entry)}   (added {entry.get('added')})")
    if queue.is_running():
        print("A download is running and will work through these when it finishes.")
    else:
        print("Nothing is running. Start them with 'qobuz-dl queue --run'.")
