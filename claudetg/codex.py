"""Codex's weekly limit, read from the log Codex already writes.

Claude Code lends the bridge a status line; Codex has nothing of the sort — no
hook to install, no cache to share, no supported command to ask. What it does
have is a rollout per session under `~/.codex/sessions/YYYY/MM/DD/`, where
every answer appends a `token_count` event carrying the same `rate_limits` its
own UI reads. The number is already on disk, so this finds the newest line
that has one.

Only the weekly window exists. Across every rollout on the machine this was
written against, `secondary` was always null and `window_minutes` always
10080, so there is no session window to show and no reason to invent a row
for one.

Nothing here reaches the network or opens Codex's credentials: it reads the
tail of a file Codex wrote anyway, and stays quiet when there is none.
"""

import datetime
import json
import os
import time

# CODEX_HOME is Codex's own override; honouring it costs one lookup and is the
# difference between working and silently reading nothing on a machine that
# moved the directory.
HOME = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"),
                                                    ".codex")

# The active rollout runs to hundreds of megabytes, so it is read from the end.
# A token_count event is small and written on every answer, so the last chunk
# nearly always holds one; the deep tail covers a session that has just
# appended something enormous — a tool output, a world-state dump — on top of
# the newest reading.
TAIL = 256 * 1024
DEEP_TAIL = 8 * 1024 * 1024
# Rollouts to look through, newest first: the newest file can belong to a pool
# that reports no window of its own, and the answer is then one file back.
CANDIDATES = 4
# The widget polls every three seconds. A weekly window does not move at that
# speed, and re-reading a quarter of a megabyte twenty times a minute is a tax
# on the disk for a number that cannot have changed meaningfully.
MIN_INTERVAL = 15
# Older than this and it is not a limit any more, it is a souvenir: better no
# row at all than a fortnight-old percentage presented as current.
MAX_AGE = 14 * 86400

_cache = {"at": 0.0, "key": None, "reading": None}


def forget():
    """Drop the cache, so the next read actually touches the disk."""
    _cache.update({"at": 0.0, "key": None, "reading": None})


def sessions_dir(home=None):
    return os.path.join(home or HOME, "sessions")


def _entries(path):
    """Directory contents, newest name first. The tree is named by date, so
    the name sorts the same way the clock does."""
    try:
        return sorted(os.listdir(path), reverse=True)
    except OSError:
        return []


def day_dirs(root, want=3):
    """The most recent day directories, newest first.

    Walked year -> month -> day rather than globbed: the archive only ever
    grows, and answering "what was written last?" should not cost a stat of
    every session ever recorded.
    """
    out = []
    for year in _entries(root):
        for month in _entries(os.path.join(root, year)):
            for day in _entries(os.path.join(root, year, month)):
                path = os.path.join(root, year, month, day)
                if os.path.isdir(path):
                    out.append(path)
                    if len(out) >= want:
                        return out
    return out


def newest_rollouts(home=None, want=CANDIDATES):
    """Recently written rollouts, newest first, each with its mtime and size.

    The stamp travels with the path so the caller can tell "nothing has been
    written since last time" from "there is something new to read" without
    opening anything.
    """
    found = []
    for day in day_dirs(sessions_dir(home)):
        for name in _entries(day):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(day, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            found.append((info.st_mtime, info.st_size, path))
    found.sort(reverse=True)
    return [(path, mtime, size) for mtime, size, path in found[:want]]


def _epoch(stamp):
    """Codex's ISO-8601 with a Z -> unix seconds."""
    if not stamp:
        return None
    try:
        return datetime.datetime.fromisoformat(
            str(stamp).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def tail_lines(path, size):
    """The last `size` bytes as whole lines.

    The first line is dropped whenever the file is longer than the window:
    seeking into the middle of a file cuts a line in half, and half a JSON
    object is not worth the exception handling downstream.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            f.seek(max(0, end - size))
            chunk = f.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", "replace").split("\n")
    return lines[1:] if end > size else lines


def limit_in(lines):
    """The last usable rate_limits record in these lines, or None.

    "Usable" rules out the pools that report themselves without a window —
    `premium` sends `primary: null` — which is not a limit anyone can watch.
    """
    for line in reversed(lines):
        if "rate_limits" not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue            # truncated or half-written: keep looking back
        limits = (record.get("payload") or {}).get("rate_limits")
        window = (limits or {}).get("primary")
        if not isinstance(window, dict) or window.get("used_percent") is None:
            continue
        return {"used_percentage": float(window["used_percent"]),
                "resets_at": window.get("resets_at"),
                "window_minutes": window.get("window_minutes"),
                "plan_type": (limits or {}).get("plan_type"),
                "captured_at": _epoch(record.get("timestamp"))}
    return None


def from_file(path):
    """The newest reading in one rollout: a short tail first, then a long one."""
    for size in (TAIL, DEEP_TAIL):
        reading = limit_in(tail_lines(path, size))
        if reading:
            return reading
        try:
            if os.path.getsize(path) <= size:
                break           # the whole file was read already; digging is moot
        except OSError:
            break
    return None


def read(home=None, now=None):
    """The current Codex weekly limit, or None when there is nothing to show.

    Shaped like the windows in `usage`: `used_percentage` and `resets_at`, so
    the widget draws it with the same code as every other meter. A percentage
    of None means the window turned over since it was written — unknown rather
    than zero, because a new week starting is not a reading.
    """
    now = now or time.time()
    if now - _cache["at"] < MIN_INTERVAL:
        return _cache["reading"]
    _cache["at"] = now
    candidates = newest_rollouts(home)
    # Nothing has been appended since the last look, so nothing can have
    # changed: skip the read rather than parse the same tail again.
    if candidates and candidates == _cache["key"]:
        return _cache["reading"]
    _cache["key"] = candidates

    reading = None
    for path, _mtime, _size in candidates:
        reading = from_file(path)
        if reading:
            break
    if reading:
        captured = reading.get("captured_at") or 0
        resets = reading.get("resets_at") or 0
        if captured and now - captured > MAX_AGE:
            reading = None                      # Codex has not run in weeks
        elif resets and resets <= now:
            reading = dict(reading, used_percentage=None, resets_at=None)
    _cache["reading"] = reading
    return reading
