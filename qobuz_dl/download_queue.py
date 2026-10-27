"""
Download queue, background downloads and the live progress view.

Start `qobuz-dl dl ...` or `qobuz-dl lucky ...` while another one is downloading
and it's added to a queue instead of running alongside it. The qobuz-dl that's
already running works through the queue, oldest first, once its own task is done.

Each entry is the command line as typed plus the folder it was typed in, and it
runs as its own qobuz-dl process, so its flags (-q, -d, --no-db, ...) and any
relative paths apply exactly as typed. An entry is only removed after it has run,
so closing qobuz-dl or a crash loses nothing: the next download, or
`qobuz-dl queue --run`, picks up what's left.

With `background = true` in the config, dl/lucky hand their work to a detached
`qobuz-dl queue --run` and show its live output (`watch`); Esc stops watching and
gives the prompt back while the download carries on. `qobuz-dl stop` ends it.

Files, all in the config folder:
  queue.jsonl  the queue, one JSON entry per line
  queue.lock   held briefly around every read or change of queue.jsonl
  worker.lock  held by the qobuz-dl that's downloading, for as long as it runs
  worker.json  who that is: its PID, and its log if it runs in the background
  logs/        one log per background session
Both locks are OS file locks, which the OS releases if a process dies, so they
can't go stale.
"""
import codecs
import glob
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
_BACKGROUND_ENV = "QOBUZ_DL_BACKGROUND"
_LOG_ENV = "QOBUZ_DL_LOG"
_KEEP_LOGS = 5
_BACKLOG_LINES = 60

# Windows process creation flags (from the Win32 API)
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def is_queued_run() -> bool:
    """True inside a qobuz-dl started from the queue: it just runs."""
    return os.environ.get(_CHILD_ENV) == "1"


def in_background() -> bool:
    """True inside a background qobuz-dl (one with no window of its own)."""
    return os.environ.get(_BACKGROUND_ENV) == "1"


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


class _KeyReader:
    """Checks, without waiting, whether Esc (or q) was pressed."""

    def __enter__(self):
        self._saved = None
        if os.name != "nt" and sys.stdin.isatty():
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def escape_pressed(self) -> bool:
        if os.name == "nt":
            import msvcrt

            while msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):  # arrow and function keys send two
                    msvcrt.getwch()
                elif key in ("\x1b", "q", "Q"):
                    return True
            return False
        if self._saved is None:  # not a terminal
            return False
        import select

        while select.select([self._fd], [], [], 0)[0]:
            if os.read(self._fd, 1) in (b"\x1b", b"q", b"Q"):
                return True
        return False


def describe(entry: dict) -> str:
    """The queued command as you'd type it."""
    return subprocess.list2cmdline(entry.get("argv", []))


def _command(entry: dict) -> list:
    if getattr(sys, "frozen", False):  # standalone build: the exe is qobuz-dl itself
        return [sys.executable, *entry["argv"]]
    return [sys.executable, "-m", "qobuz_dl", *entry["argv"]]


def _run(entry: dict) -> int:
    """Run a queued command in its own qobuz-dl process, in the folder it was
    typed in, with its output alongside this one's."""
    cwd = entry.get("cwd")
    if not (cwd and os.path.isdir(cwd)):
        cwd = None
    env = dict(os.environ, **{_CHILD_ENV: "1"})
    kwargs = {}
    if in_background():
        # Hand the download this process's output, the session log, explicitly.
        # On Windows it gets a console of its own (hidden by CREATE_NO_WINDOW),
        # and a process with its own console writes to that console, out of
        # sight, unless it's given somewhere else to write. Nobody is there to
        # answer a question either, so its input is empty.
        sys.stdout.flush()
        kwargs.update(stdin=subprocess.DEVNULL, stdout=sys.stdout,
                      stderr=subprocess.STDOUT)
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW  # don't pop up a console window
    return subprocess.run(_command(entry), cwd=cwd, env=env, **kwargs).returncode


def _collapse(text: str) -> str:
    """What a terminal would show for text with progress bars redrawn using \\r."""
    lines = [line.rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n")]
    return "\n".join(lines[-_BACKLOG_LINES:])


class DownloadQueue:
    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.folder = folder
        self.path = os.path.join(folder, "queue.jsonl")
        self.info_path = os.path.join(folder, "worker.json")
        self.logs = os.path.join(folder, "logs")
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

    def _append(self, argv, cwd):
        entry = {
            "id": uuid.uuid4().hex,
            "argv": list(argv),
            "cwd": cwd,
            "added": time.strftime("%Y-%m-%d %H:%M"),
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def entries(self) -> list:
        with self._locked():
            return self._read()

    def clear(self) -> int:
        with self._locked():
            count = len(self._read())
            self._write([])
        return count

    # --- who's downloading ----------------------------------------------------
    def _worker_held(self) -> bool:
        probe = _FileLock(self._worker_lock.path)
        if probe.acquire(blocking=False):
            probe.release()
            return False
        return True

    def _became_worker(self):
        self.is_worker = True
        info = {"pid": os.getpid(), "background": in_background(),
                "log": os.environ.get(_LOG_ENV) if in_background() else None}
        with open(self.info_path, "w", encoding="utf-8") as f:
            json.dump(info, f)
        if in_background():
            self._prune_logs(keep=info["log"])

    def worker_info(self):
        """{"pid", "background", "log"} for the qobuz-dl that's downloading, or None."""
        if not self.is_running():
            return None
        try:
            with open(self.info_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}  # running, but it hasn't said who it is yet

    def enqueue_if_busy(self, argv, cwd) -> int:
        """If nothing else is downloading, make this process the worker and
        return 0. Otherwise add the command to the queue for the qobuz-dl that is,
        and return its position."""
        with self._locked():
            if self._worker_lock.acquire(blocking=False):
                self._became_worker()
                return 0
            self._append(argv, cwd)
            return len(self._read())

    def enqueue(self, argv, cwd):
        """Add a command to the queue. Returns (position, running): whether a
        qobuz-dl is downloading, and so will get to it."""
        with self._locked():
            self._append(argv, cwd)
            # Checked under the queue lock: a worker gives up its slot under the
            # same lock, so "running" can't change between the two.
            return len(self._read()), self._worker_held()

    def claim(self) -> bool:
        """Become the worker if nothing else is downloading."""
        with self._locked():
            if self._worker_lock.acquire(blocking=False):
                self._became_worker()
        return self.is_worker

    def is_running(self) -> bool:
        """Whether a qobuz-dl is downloading (and so will work through the queue)."""
        return self.is_worker or self._worker_held()

    def release(self):
        if self.is_worker:
            try:
                os.remove(self.info_path)
            except OSError:
                pass
            self._worker_lock.release()
            self.is_worker = False

    # --- running the queue ----------------------------------------------------
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

    # --- background downloads ---------------------------------------------------
    def start_background(self) -> str:
        """Start a detached `qobuz-dl queue --run` writing to a new session log,
        and return the log's path. If another worker beats it to the slot, it
        just exits."""
        os.makedirs(self.logs, exist_ok=True)
        name = f"session-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.log"
        log_path = os.path.join(self.logs, name)
        env = dict(os.environ, **{_BACKGROUND_ENV: "1", _LOG_ENV: log_path,
                                  "PYTHONUNBUFFERED": "1"})
        env.pop(_CHILD_ENV, None)
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        with open(log_path, "ab") as log:
            subprocess.Popen(
                _command({"argv": ["queue", "--run"]}), cwd=self.folder, env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                **kwargs,
            )
        return log_path

    def _prune_logs(self, keep):
        logs = sorted(glob.glob(os.path.join(self.logs, "session-*.log")),
                      key=os.path.getmtime)
        for old in logs[:-_KEEP_LOGS]:
            if old != keep:
                try:
                    os.remove(old)
                except OSError:
                    pass  # still open somewhere; it'll go next time

    def _latest_log(self):
        logs = glob.glob(os.path.join(self.logs, "session-*.log"))
        return max(logs, key=os.path.getmtime) if logs else None

    def watch(self, wait=0.0, keys=None, out=None, poll=0.2):
        """Show the background download's output as it happens. Esc (or q, or
        Ctrl+C) stops watching; the download carries on. Returns by itself once
        there's nothing left to download. `wait` gives a just-started background
        qobuz-dl that long to get going."""
        out = out or sys.stdout
        deadline = time.time() + max(wait, 2.0)
        info = self.worker_info()
        # {} is a worker that hasn't said who it is yet (it's just starting or
        # just finishing); give it a moment either way.
        while (info == {} or (wait and not (info and info.get("log")))) and (
            time.time() < deadline
        ):
            time.sleep(0.1)
            info = self.worker_info()

        if info is not None and not info.get("background"):
            print("The download that's running is in another window; there's nothing "
                  "to watch here.")
            return
        log_path = (info or {}).get("log") or self._latest_log()
        if not log_path or not os.path.exists(log_path):
            print("Nothing has run in the background yet.")
            return

        log = open(log_path, "rb")
        # One decoder for the backlog and the live part, so a character whose
        # bytes are split between the two isn't mangled.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            out.write(_collapse(decoder.decode(log.read(), final=info is None)))
            out.flush()
            if info is None:
                print(f"\n{GREEN}[watch] Nothing is downloading right now.{OFF}")
                return
            print(f"\n{YELLOW}[watch] Live progress. Press Esc to stop watching; "
                  f"the download carries on.{OFF}")
            with (keys or _KeyReader()) as key_reader:
                while True:
                    chunk = log.read()
                    if chunk:
                        out.write(decoder.decode(chunk))
                        out.flush()
                    if key_reader.escape_pressed():
                        print(f"\n{YELLOW}[watch] Stopped watching. The download "
                              "carries on; 'qobuz-dl watch' shows it again.{OFF}")
                        return
                    info = self.worker_info()
                    if info is None:
                        out.write(decoder.decode(log.read(), final=True))
                        print(f"\n{GREEN}[watch] All done: nothing left to "
                              f"download.{OFF}")
                        return
                    new_log = info.get("log")
                    if new_log and new_log != log_path and os.path.exists(new_log):
                        log.close()  # a new background session took over
                        log_path, log = new_log, open(new_log, "rb")
                        decoder = codecs.getincrementaldecoder("utf-8")("replace")
                    if not chunk:
                        time.sleep(poll)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}[watch] Stopped watching. The download carries on; "
                  f"'qobuz-dl watch' shows it again.{OFF}")
        finally:
            log.close()

    def stop(self) -> bool:
        """Stop the qobuz-dl that's downloading, and whatever it started. What
        was queued stays queued. Returns False if nothing was running."""
        info = self.worker_info()
        if info is None:
            return False
        pid = info.get("pid")
        if pid:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True)
            else:
                import signal

                try:
                    if info.get("background"):  # it leads its own process group
                        os.killpg(os.getpgid(pid), signal.SIGTERM)
                    else:
                        os.kill(pid, signal.SIGINT)  # as if Ctrl+C in its window
                except (ProcessLookupError, PermissionError):
                    pass
        for _ in range(100):  # wait for the slot to be released
            if not self._worker_held():
                break
            time.sleep(0.1)
        return True


def queue_command(queue: DownloadQueue, run=False, clear=False):
    """`qobuz-dl queue`: show the queue, or --clear it, or --run it now."""
    if clear:
        print(f"{GREEN}[queue] Removed {queue.clear()} item(s).{OFF}")
        return
    entries = queue.entries()
    if not entries:
        if not in_background():
            print("[queue] Nothing queued.")
        return
    if run:
        if queue.claim():
            queue.run_all()
        elif not in_background():  # a background starter that lost the race: silent
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
