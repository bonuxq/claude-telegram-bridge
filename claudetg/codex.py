"""Codex's weekly limit, read from the log Codex already writes.

Claude Code lends the bridge a status line; Codex has nothing of the sort — no
hook to install, no cache to share, no supported command to ask. What it does
have is a rollout per session under `~/.codex/sessions/YYYY/MM/DD/`, where
every answer appends a `token_count` event carrying the same `rate_limits` its
own UI reads. The number is already on disk, so this finds the newest line
that has one.

The row is the plan's weekly window. Codex reports one pool per model on top
of the plan's own, and a per-model pool carries a 5-hour window too, but the
plan pool is the number its UI shows and the week is the limit that bites.

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
CANDIDATES = 6
# How far back the day directories are walked for those. Codex creates a
# rollout the moment a window opens, so a quiet week leaves a few empty files
# in the newest days with the last real reading days behind them.
DAYS = 30
# The widget polls every three seconds. A weekly window does not move at that
# speed, and re-reading a quarter of a megabyte twenty times a minute is a tax
# on the disk for a number that cannot have changed meaningfully.
MIN_INTERVAL = 15
# Older than this and it is not a limit any more, it is a souvenir: better no
# row at all than a fortnight-old percentage presented as current.
MAX_AGE = 14 * 86400
# The plan's own pool — the number Codex's own UI shows. Everything else is
# `codex_<model>`: a per-model allowance that sits at zero unless that model
# is the one being used.
MAIN_POOL = "codex"
WEEK_MINUTES = 10080

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


def day_dirs(root, want=DAYS):
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
            # A rollout is created when a window opens and stays empty until
            # something is said in it. Those hold nothing to read, and every
            # one of them would otherwise use up a candidate slot.
            if info.st_size > 0:
                found.append((info.st_mtime, info.st_size, path))
        if len(found) >= want:
            break               # the days are newest first: enough already
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


def weekly_window(limits):
    """The weekly window of one pool, whichever slot it arrived in.

    Which slot holds which window is not fixed: the plan pool sends its week
    as `primary` and nothing else, while a per-model pool sends a 5-hour
    window there and the week in `secondary`. The length says what a window
    is; its slot does not. Anything without a percentage is not a limit
    anyone can watch — `premium` sends `primary: null` — and is skipped.
    """
    windows = [w for w in (limits.get("primary"), limits.get("secondary"))
               if isinstance(w, dict) and w.get("used_percent") is not None]
    weekly = [w for w in windows if w.get("window_minutes") == WEEK_MINUTES]
    if weekly:
        return weekly[0]
    # No week in this pool: the longest window it does have is the closest
    # thing to one, and reporting nothing would be worse.
    return max(windows, key=lambda w: w.get("window_minutes") or 0, default=None)


def limit_in(lines):
    """The newest usable reading in these lines, preferring the plan's pool.

    Codex reports every pool it knows about, one record each, within the same
    second. A per-model pool that has never been touched reports zero, and
    one of those landing last is not a reason to say the plan is empty — so
    the main pool wins, and the rest are only a fallback for the day this
    format changes again.
    """
    fallback = None
    for line in reversed(lines):
        if "rate_limits" not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue            # truncated or half-written: keep looking back
        limits = (record.get("payload") or {}).get("rate_limits") or {}
        window = weekly_window(limits)
        if window is None:
            continue
        reading = {"used_percentage": float(window["used_percent"]),
                   "resets_at": window.get("resets_at"),
                   "window_minutes": window.get("window_minutes"),
                   "pool": limits.get("limit_id"),
                   "plan_type": limits.get("plan_type"),
                   "captured_at": _epoch(record.get("timestamp"))}
        if limits.get("limit_id") == MAIN_POOL:
            return reading
        if fallback is None:
            fallback = reading
    return fallback


def from_file(path):
    """The newest reading in one rollout: a short tail first, then a long one.

    A per-model pool is written as often as the plan's own, so a short tail
    can easily hold nothing but those. Finding one is a reason to dig deeper,
    not an answer — but it is kept, in case the deeper read finds no more.
    """
    reading = None
    for size in (TAIL, DEEP_TAIL):
        found = limit_in(tail_lines(path, size))
        if found and found.get("pool") == MAIN_POOL:
            return found
        reading = reading or found
        try:
            if os.path.getsize(path) <= size:
                break           # the whole file was read already; digging is moot
        except OSError:
            break
    return reading


def read(home=None, now=None):
    """The current Codex weekly limit, or None when there is nothing to show.

    Shaped like the windows in `usage`: `used_percentage` and `resets_at`, so
    the widget draws it with the same code as every other meter. A window that
    turned over since it was written reads as zero with no reset time: the
    week it described has ended, nothing has been spent in the new one or
    there would be a newer record, and a dash where a number belongs reads as
    "broken", not as "fresh".
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
        found = from_file(path)
        if found and found.get("pool") == MAIN_POOL:
            reading = found
            break               # the plan's own pool: nothing beats it
        reading = reading or found
    if reading:
        captured = reading.get("captured_at") or 0
        resets = reading.get("resets_at") or 0
        if captured and now - captured > MAX_AGE:
            reading = None                      # Codex has not run in weeks
        elif resets and resets <= now:
            reading = dict(reading, used_percentage=0.0, resets_at=None,
                           reset=True)
    _cache["reading"] = reading
    return reading
