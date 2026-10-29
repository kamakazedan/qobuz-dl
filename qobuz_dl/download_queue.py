"""
Download queue, background downloads and the live progress view.

Start `qobuz-dl dl ...` or `qobuz-dl lucky ...` while another one is downloading
and it's added to a queue instead of running alongside it. The qobuz-dl that's
already running works through the queue, oldest first, once its own task is done.

Each entry is the command line as typed plus the folder it was typed in, and it
runs as its own qobuz-dl process, so its flags (-q, -d, --no-db, ...) and any
relative paths apply exactly as typed. An entry is only removed after it has run,
so closing qobuz-dl or a crash loses nothing: the next download, `qobuz-dl
start` or `qobuz-dl queue --run` picks up what's left. A command that's queued
already isn't added again, and `qobuz-dl queue --remove N` (or `last`) takes one
out.

With `background = true` in the config, dl/lucky hand their work to a detached
`qobuz-dl queue --run` and give the prompt straight back. `qobuz-dl watch` (or
--watch on the command) shows its live output until Esc, carrying on to the next
download when one starts; the download carries on regardless. `qobuz-dl stop`
ends it, and `qobuz-dl start` carries on with the queue in the background.

`qobuz-dl status` sums it up: what's downloading, how far it's got (from the
background log), and what's queued. `status --watch` keeps that on screen,
updated every second, until Esc.

Files, all in the config folder:
  queue.jsonl  the queue, one JSON entry per line
  queue.lock   held briefly around every read or change of queue.jsonl
  worker.lock  held by the qobuz-dl that's downloading, for as long as it runs
  worker.json  who that is: its PID, its log if it runs in the background, and
               the command it's running now
  logs/        one log per background session
Both locks are OS file locks, which the OS releases if a process dies, so they
can't go stale.
"""
import codecs
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
from contextlib import contextmanager

from qobuz_dl.color import CYAN, GREEN, YELLOW, OFF, RESET

# Commands that wait their turn. Interactive mode needs you at the keyboard, so
# it never queues and isn't held up by the queue.
QUEUED_COMMANDS = ("dl", "lucky")
# Commands that look at or manage the queue: quick, and no Qobuz login needed
QUEUE_COMMANDS = ("queue", "status", "watch", "stop", "start")
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

    @property
    def interactive(self) -> bool:
        """Whether there's a keyboard to press Esc on (input isn't redirected)."""
        if os.name == "nt":
            try:
                return sys.stdin is not None and sys.stdin.isatty()
            except (AttributeError, ValueError):
                return False
        return self._saved is not None

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


def _position(entries, argv, cwd) -> int:
    """Where the same command, typed in the same folder, is in the queue (1 is
    first), or 0 if it isn't."""
    for number, entry in enumerate(entries, 1):
        if entry.get("argv") == list(argv) and entry.get("cwd") == cwd:
            return number
    return 0


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


class _Log:
    """A background session's log, read as it grows. One decoder for all of it,
    so a character whose bytes arrive in two reads isn't mangled."""

    def __init__(self, path):
        self.path = path
        self._file = open(path, "rb")
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def read(self, final=False) -> str:
        return self._decoder.decode(self._file.read(), final=final)

    def close(self):
        self._file.close()


def _stopped_watching(state) -> str:
    if state == "idle":
        return f"\n{YELLOW}[watch] Stopped watching.{OFF}"
    return (f"\n{YELLOW}[watch] Stopped watching. The download carries on; "
            f"'qobuz-dl watch' shows it again.{OFF}")


def _replace(tmp, path):
    for attempt in range(5):  # a virus scanner or a reader can hold the file for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2)


# --- reading a download's output, for `qobuz-dl status` ------------------------
_STARTING = "[queue] Starting: "
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_BAR_RE = re.compile(r"(\d{1,3})%\|")  # a progress bar: " Downloading:  76%|███▌ |..."
_SOURCE_RE = re.compile(r"^Downloading all the music from (.+) \((\w+)\)!$")
_RELEASE_RE = re.compile(r"^(?:\[(\d+)/(\d+)\] )?Downloading: (.+)$")
_QUALITY_RE = re.compile(r"^Quality: (.+)$")
_FILE_RE = re.compile(r"^\[\+\] In progress: (.+?)(?: \[[\d.]+ MB\])?$")
_FILE_DONE_RE = re.compile(r"^L Completed: (.+)$")
_TRACK_FILE_RE = re.compile(r"^\d+\. ")  # tracks are "06. Title"; covers and booklets aren't
_RELEASE_END_RE = re.compile(
    r"^(?:Completed$|\[!\] Album downloaded partially|\[!\] Download aborted)")
_STATUS_TAIL_BYTES = 2_000_000


def read_progress(text: str) -> dict:
    """How far a download has got, from its output: "source" (the artist, label or
    playlist) and "kind" (which of those), "number" and "total" (which of its
    releases), "release", "quality", "file" (the track being downloaded),
    "percent", "file_done", "tracks_done", "release_done", and "last" (the latest
    line). Only the latest queued command counts."""
    start = text.rfind(_STARTING)
    if start != -1:
        text = text[start:]
    progress = {}
    for line in _ANSI_RE.sub("", text).split("\n"):
        for part in line.split("\r"):
            part = part.strip()
            if not part:
                continue
            bar = _BAR_RE.search(part)
            if bar:
                if progress.get("file"):
                    progress["percent"] = min(int(bar.group(1)), 100)
                continue
            progress["last"] = part
            if part.startswith(_STARTING):
                progress = {"last": part}
            elif _SOURCE_RE.match(part):
                progress["source"], progress["kind"] = _SOURCE_RE.match(part).groups()
            elif _RELEASE_RE.match(part):
                number, total, title = _RELEASE_RE.match(part).groups()
                for key in ("quality", "file", "percent", "file_done", "release_done"):
                    progress.pop(key, None)
                progress.update(release=title, number=number and int(number),
                                total=total and int(total), tracks_done=0)
            elif _QUALITY_RE.match(part):
                progress["quality"] = _QUALITY_RE.match(part).group(1)
            elif _FILE_RE.match(part):
                progress.update(file=_FILE_RE.match(part).group(1), file_done=False)
                progress.pop("percent", None)
            elif _FILE_DONE_RE.match(part):
                name = _FILE_DONE_RE.match(part).group(1)
                if name == progress.get("file"):
                    progress["file_done"] = True
                if _TRACK_FILE_RE.match(name):
                    progress["tracks_done"] = progress.get("tracks_done", 0) + 1
            elif _RELEASE_END_RE.match(part):
                progress["release_done"] = True
    return progress


def _read_tail(path, size=_STATUS_TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - size))
            data = f.read()
    except OSError:
        return ""
    if len(data) == size:  # started part-way through a line: drop that line
        data = data[data.find(b"\n") + 1:]
    return data.decode("utf-8", "replace")


def _progress_lines(progress: dict) -> list:
    """read_progress() as a couple of lines for `qobuz-dl status`."""
    if not progress.get("release"):
        last = progress.get("last", "")
        if not last or last.startswith(_STARTING):
            return ["Getting started."]
        return [f"Latest: {last}"]  # still looking up what to download
    quality = progress.get("quality")
    if progress.get("total"):
        what = f"{'Release' if quality else 'Track'} {progress['number']} of {progress['total']}"
    else:
        what = "Album" if quality else "Track"
    line = f"{what}: {progress['release']}" + (f", {quality}" if quality else "")
    if progress.get("release_done"):
        return [line + ", done"]
    lines = [line]
    tracks = progress.get("tracks_done", 0)
    done = f" ({tracks} track{'s' if tracks != 1 else ''} done)" if tracks else ""
    if progress.get("file"):
        if progress.get("file_done"):
            lines.append(f"Just finished: {progress['file']}{done}")
        else:
            percent = progress.get("percent")
            lines.append(f"Now: {progress['file']}"
                         + (f", {percent}%" if percent is not None else "") + done)
    return lines


def _report(snapshot: dict, max_queued=None) -> list:
    """The status as (colour, text) lines. max_queued: list at most that many of
    the queued commands."""
    info, current, waiting = snapshot["info"], snapshot["current"], snapshot["waiting"]
    if info is None and not waiting:
        return [(GREEN, "[status] Nothing is downloading, and nothing is queued.")]
    lines = []
    if info is None:
        lines.append((YELLOW, "[status] Nothing is downloading."))
    elif info == {}:
        lines.append((CYAN, "[status] A download is just starting."))
    else:
        where = "in the background" if info.get("background") else "in another window"
        since = current.get("since")
        took = f", for {_duration(time.time() - since)}" if since else ""
        lines.append((CYAN, f"[status] Downloading {where}{took}:"))
        progress = snapshot["progress"]
        if not current.get("argv"):
            lines.append(("", "  Getting the next download ready."))
        elif progress is not None:
            name = f"   ({progress['source']})" if progress.get("source") else ""
            lines.append(("", f"  qobuz-dl {describe(current)}{name}"))
            lines += [("", f"  {line}") for line in _progress_lines(progress)]
        else:
            lines.append(("", f"  qobuz-dl {describe(current)}"))
            lines.append(("", "  (that window shows how far it's got)"))
    if waiting:
        heading = ("Queued next" if info is not None
                   else "Queued, waiting for 'qobuz-dl start'")
        lines.append((YELLOW, f"[status] {heading} ({len(waiting)}):"))
        listed = waiting if max_queued is None else waiting[:max_queued]
        for number, entry in enumerate(listed, 1):
            number = entry.get("_position", number)
            lines.append(("", f"  {number}. qobuz-dl {describe(entry)}   "
                              f"(added {_when(entry.get('added'))})"))
        if len(listed) < len(waiting):
            lines.append(("", f"  … and {len(waiting) - len(listed)} more"))
    elif info is not None:
        lines.append(("", "[status] Nothing else queued."))
    return lines


def _paint(colour, text) -> str:
    return f"{colour}{text}{RESET}" if colour else text


def _is_terminal(stream) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _cells(text) -> int:
    """How many columns text takes up in a terminal (CJK characters take two)."""
    return sum(0 if unicodedata.combining(ch)
               else 2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _fit(text, width) -> str:
    """text cut to `width` columns, ending in … if it had to be cut."""
    if _cells(text) <= width:
        return text
    kept, used = [], 0
    for ch in text:
        used += _cells(ch)
        if used > width - 1:
            break
        kept.append(ch)
    return "".join(kept) + "…"


def _rows(lines, width) -> int:
    """Terminal rows that (colour, text) lines take up at `width` columns. They're
    cut to fit when drawn, but the window may have been narrowed since."""
    width = max(1, width)
    return sum(max(1, -(-_cells(text) // width)) for _, text in lines)


def _frame(snapshot, footer, width, height) -> list:
    """The live status as it fits the terminal: no line wider than it (so none
    wraps), and few enough lines that it never scrolls; a long queue is shortened."""
    room = max(2, height - 1)  # the cursor waits on the row below
    lines = _report(snapshot)
    if len(lines) + 1 > room and snapshot["waiting"]:
        keep = len(snapshot["waiting"]) - (len(lines) + 1 - room) - 1  # -1: "… and N more"
        lines = _report(snapshot, max_queued=max(0, keep))
    lines = lines[:room - 1] + [footer]
    return [(colour, _fit(text, width - 1)) for colour, text in lines]


def _redraw(lines, up) -> str:
    """Go back up `up` rows to the previous update, draw the lines over it, and
    clear anything of it that's left below."""
    return (f"\r\x1b[{up}A" if up else "\r") + "".join(
        _paint(colour, text) + "\x1b[K\n" for colour, text in lines) + "\x1b[J"


def _duration(seconds) -> str:
    minutes = int(max(0, seconds)) // 60
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def _when(added) -> str:
    """'18:31' for something added today, else the date too."""
    added = added or ""
    today = time.strftime("%Y-%m-%d ")
    return added[len(today):] if added.startswith(today) else added


class DownloadQueue:
    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.folder = folder
        self.path = os.path.join(folder, "queue.jsonl")
        self.info_path = os.path.join(folder, "worker.json")
        self.logs = os.path.join(folder, "logs")
        self._queue_lock = _FileLock(os.path.join(folder, "queue.lock"))
        self._worker_lock = _FileLock(os.path.join(folder, "worker.lock"))
        self._holding_queue_lock = False
        self._info = {}
        self.is_worker = False

    # --- the queue file (only touched while holding the queue lock) ---------
    @contextmanager
    def _locked(self):
        self._queue_lock.acquire()
        self._holding_queue_lock = True
        try:
            yield
        finally:
            self._holding_queue_lock = False
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
        _replace(tmp, self.path)

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
        # Checking means taking the worker slot for a moment, so it's done under
        # the queue lock, as taking the slot for real is: a qobuz-dl that's just
        # starting can't then mistake the check for a running worker and give up.
        if not self._holding_queue_lock:
            with self._locked():
                return self._worker_held()
        probe = _FileLock(self._worker_lock.path)
        if probe.acquire(blocking=False):
            probe.release()
            return False
        return True

    def _became_worker(self, argv=None):
        """argv: the command this qobuz-dl is about to run itself, if any."""
        self.is_worker = True
        self._info = {"pid": os.getpid(), "background": in_background(),
                      "log": os.environ.get(_LOG_ENV) if in_background() else None,
                      "current": None}
        if argv:
            self._set_current({"argv": list(argv)})
        else:
            self._write_info()
        if in_background():
            self._prune_logs(keep=self._info["log"])

    def _set_current(self, entry):
        """Note the command being run now, for `qobuz-dl status`."""
        self._info["current"] = {"id": entry.get("id"), "argv": entry.get("argv", []),
                                 "since": time.time()}
        self._write_info()

    def _write_info(self):
        tmp = self.info_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._info, f)
            _replace(tmp, self.info_path)
        except OSError:
            pass  # only status and watch read it, and they cope without

    def worker_info(self):
        """{"pid", "background", "log", "current"} for the qobuz-dl that's
        downloading, or None. "current" is {"id", "argv", "since"}: what it's running."""
        if not self.is_running():
            return None
        try:
            with open(self.info_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}  # running, but it hasn't said who it is yet

    def enqueue_if_busy(self, argv, cwd) -> int:
        """If nothing else is downloading, make this process the worker and
        return 0 (a copy of the same command waiting in the queue is dropped:
        it's running now). Otherwise add the command to the queue for the
        qobuz-dl that is, unless it's queued already, and return its position."""
        with self._locked():
            entries = self._read()
            position = _position(entries, argv, cwd)
            if self._worker_lock.acquire(blocking=False):
                if position:
                    del entries[position - 1]
                    self._write(entries)
                self._became_worker(argv)
                return 0
            if position:
                return position
            self._append(argv, cwd)
            return len(entries) + 1

    def enqueue(self, argv, cwd):
        """Add a command to the queue, unless the same command (typed in the same
        folder) is queued already. Returns (position, running, added): its place
        in the queue, whether a qobuz-dl is downloading (and so will get to it),
        and whether it was added just now."""
        with self._locked():
            entries = self._read()
            position = _position(entries, argv, cwd)
            if not position:
                self._append(argv, cwd)
                position = len(entries) + 1
            # Checked under the queue lock: a worker gives up its slot under the
            # same lock, so "running" can't change between the two.
            return position, self._worker_held(), position > len(entries)

    def _current(self, info, entries) -> dict:
        """The command being downloaded now ({} if none): "argv", and "id" if it
        came from the queue."""
        current = (info or {}).get("current") or {}
        if info and info.get("background") and not current and entries:
            current = entries[0]  # a background qobuz-dl runs the queue in order
        return current

    def remove(self, which):
        """Take a command out of the queue: `which` is its position, as `qobuz-dl
        queue` numbers them, or "last" for the one added most recently. Returns
        (outcome, entry), outcome being "removed", "running" (it's downloading
        now, so it stays), "empty" or "missing" (no such position)."""
        with self._locked():
            entries = self._read()
            if not entries:
                return "empty", None
            if str(which).strip().lower() == "last":
                number = len(entries)
            else:
                try:
                    number = int(which)
                except (TypeError, ValueError):
                    return "missing", None
            if not 1 <= number <= len(entries):
                return "missing", None
            entry = entries[number - 1]
            running = self._current(self.worker_info(), entries).get("id")
            if running and entry.get("id") == running:
                return "running", entry
            del entries[number - 1]
            self._write(entries)
            return "removed", entry

    def start(self) -> str:
        """`qobuz-dl start`: work through the queue in the background, e.g. after
        `qobuz-dl stop`. Returns "empty" if nothing is queued, "running" if a
        qobuz-dl is downloading already (it works through the queue itself),
        "started", or "slow" if the background one hasn't got going within 15
        seconds."""
        with self._locked():
            if not self._read():
                return "empty"
            if self._worker_held():
                return "running"
        self.start_background()
        return "started" if self.wait_for_worker() else "slow"

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
                    # Noted under the queue lock, so `queue --remove` can't take
                    # it out of the queue once it's its turn
                    self._set_current(entry)
                print(
                    f"\n{CYAN}{_STARTING}qobuz-dl {describe(entry)}"
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
                        "'qobuz-dl start' carries on in the background ('qobuz-dl "
                        f"queue --run' in this window), 'qobuz-dl queue --clear' drops "
                        f"them.{OFF}"
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

    def wait_for_worker(self, timeout=15.0, poll=0.1) -> bool:
        """After start_background(): give it up to `timeout` seconds to get going.
        True once a qobuz-dl is downloading, or the queue has already been run."""
        deadline = time.time() + timeout
        while not (self.is_running() or not self.entries()):
            if time.time() >= deadline:
                return False
            time.sleep(poll)
        return True

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
        """Show the background download's output as it happens, until Esc (or q,
        or Ctrl+C); the download carries on. When there's nothing left to
        download it keeps watching, and follows the next background download
        when one starts. Without a keyboard to press Esc on, it returns once
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

        log = None  # the background session being followed
        if info is None or info.get("background"):
            path = (info or {}).get("log") or self._latest_log()
            if path and os.path.exists(path):
                log = _Log(path)
                out.write(_collapse(log.read(final=info is None)))
                out.flush()
        # "live": following a background download; "idle": nothing downloading;
        # "window": a download is running in another window (which shows it)
        state, window = "live", None
        try:
            with (keys or _KeyReader()) as key_reader:
                until_esc = getattr(key_reader, "interactive", False)
                if info is None:
                    state = "idle"
                    print(f"\n{GREEN}[watch] Nothing is downloading right now.{OFF}" if log
                          else "Nothing has run in the background yet.")
                elif not info.get("background"):
                    state, window = "window", info.get("pid")
                    print(f"{YELLOW}[watch] The download that's running is in another "
                          f"window, which shows its progress.{OFF}" if until_esc
                          else "The download that's running is in another window; "
                          "there's nothing to watch here.")
                else:
                    print(f"\n{YELLOW}[watch] Live progress. Press Esc to stop watching; "
                          f"the download carries on.{OFF}")
                if state != "live":
                    if not until_esc:
                        return
                    print(f"{YELLOW}[watch] Watching for the next download. Press Esc "
                          f"to stop watching.{OFF}")
                while True:
                    chunk = log.read() if log else ""
                    if chunk:
                        out.write(chunk)
                        out.flush()
                    if key_reader.escape_pressed():
                        print(_stopped_watching(state))
                        return
                    info = self.worker_info()
                    if info is None:
                        if state != "idle":
                            if log:
                                out.write(log.read(final=True))
                                out.flush()
                            print(f"\n{GREEN}[watch] All done: nothing left to "
                                  f"download.{OFF}")
                            if not until_esc:
                                return
                            print(f"{YELLOW}[watch] Still watching for the next "
                                  f"download. Press Esc to stop watching.{OFF}")
                            state = "idle"
                    elif info.get("background") and info.get("log"):
                        if (not log or info["log"] != log.path) and os.path.exists(info["log"]):
                            if log:  # a new background session: finish the old one first
                                out.write(log.read(final=True))
                                log.close()
                            log = _Log(info["log"])
                        state = "live"
                    elif info and not info.get("background"):
                        if state != "window" or info.get("pid") != window:
                            print(f"\n{YELLOW}[watch] A download is running in another "
                                  f"window, which shows its progress.{OFF}")
                        state, window = "window", info.get("pid")
                    if not chunk:
                        time.sleep(poll)
        except KeyboardInterrupt:
            print(_stopped_watching(state))
        finally:
            if log:
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

    def _snapshot(self) -> dict:
        """What `qobuz-dl status` shows, read afresh."""
        info = self.worker_info()
        entries = self.entries()
        current = self._current(info, entries)
        progress = None  # how far it's got: only known for background downloads
        if info and info.get("background") and current.get("argv"):
            progress = read_progress(_read_tail(info.get("log") or ""))
        # Numbered as in `qobuz-dl queue`, so the numbers work with --remove
        waiting = [dict(e, _position=n) for n, e in enumerate(entries, 1)
                   if not current.get("id") or e.get("id") != current["id"]]
        return {"info": info, "current": current, "progress": progress, "waiting": waiting}

    def status(self, out=None, live=False, keys=None, interval=1.0, poll=0.1):
        """`qobuz-dl status`: what's downloading, how far it's got, and what's
        queued. How far it's got comes from the background log, so for a download
        running in another window it's just what it's running.

        live (`status --watch`): keep it on screen, updated every `interval`
        seconds, until Esc (or q, or Ctrl+C). Needs a terminal; elsewhere the
        status is shown once."""
        out = out or sys.stdout
        if live and _is_terminal(out):
            self._live_status(out, keys or _KeyReader(), interval, poll)
            return
        for colour, text in _report(self._snapshot()):
            out.write(_paint(colour, text) + "\n")

    def _live_status(self, out, keys, interval, poll):
        shown = []  # the lines on screen, to draw the next update over
        try:
            with keys as key_reader:
                next_update = 0.0
                while not key_reader.escape_pressed():
                    if time.monotonic() >= next_update:
                        width, height = shutil.get_terminal_size()
                        footer = ("", f"[status] Updated {time.strftime('%H:%M:%S')}. "
                                      "Press Esc to stop watching.")
                        frame = _frame(self._snapshot(), footer, width, height)
                        out.write(_redraw(frame, _rows(shown, width)))
                        out.flush()
                        shown, next_update = frame, time.monotonic() + interval
                    time.sleep(poll)
        except KeyboardInterrupt:
            pass
        if shown:  # the last update stays on screen; its footer makes way for this
            out.write("\x1b[1A\r\x1b[K")
        out.write(_paint(YELLOW, "[status] Stopped watching.") + "\n")
        out.flush()


def queue_command(queue: DownloadQueue, run=False, clear=False, remove=None):
    """`qobuz-dl queue`: show the queue, or --clear it, --remove one item, or
    --run it now."""
    if clear:
        print(f"{GREEN}[queue] Removed {queue.clear()} item(s).{OFF}")
        return
    if remove is not None:
        outcome, entry = queue.remove(remove)
        if outcome == "removed":
            print(f"{GREEN}[queue] Removed: qobuz-dl {describe(entry)}{OFF}")
        elif outcome == "running":
            print(f"{YELLOW}[queue] That one is downloading right now. To drop it: "
                  f"'qobuz-dl stop', then 'qobuz-dl queue --remove {remove}', then "
                  f"'qobuz-dl start' for the rest.{OFF}")
        elif outcome == "empty":
            print("[queue] Nothing queued.")
        else:
            print(f"[queue] There's no item {remove} in the queue; 'qobuz-dl queue' "
                  "lists them.")
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
    running = queue._current(queue.worker_info(), entries).get("id")
    print(f"[queue] {len(entries)} item(s) queued:")
    for number, entry in enumerate(entries, 1):
        note = ("downloading now" if running and entry.get("id") == running
                else f"added {entry.get('added')}")
        print(f"  {number}. qobuz-dl {describe(entry)}   ({note})")
    if queue.is_running():
        print("A download is running and will work through these when it finishes.")
    else:
        print("Nothing is running. 'qobuz-dl start' starts them in the background, "
              "'qobuz-dl queue --run' in this window.")
    print("'qobuz-dl queue --remove N' takes one out ('--remove last': the newest).")
