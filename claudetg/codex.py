"""Codex's weekly limit: asked of the usage endpoint its own CLI uses, and
read from the logs it already writes when that is not possible.

The endpoint is the truth. A rollout only records the limit as it stood after
a request *from this machine*, so a week spent from a phone, a browser or a
second computer never shows up in one — the card sat on 90% while the plan
was at 100%. `GET chatgpt.com/backend-api/wham/usage`, with the token the CLI
keeps in `~/.codex/auth.json`, answers for the account rather than the
machine; it is what `codex` itself reads for `/status`.

The logs are the fallback: a rollout per session under
`~/.codex/sessions/YYYY/MM/DD/` (moved to `archived_sessions/` once closed),
where every answer appends a `token_count` event carrying the same
`rate_limits` the UI shows. No network, no token — and no news from anywhere
else.

The row is the plan's weekly window. Codex reports one pool per model on top
of the plan's own, and a per-model pool carries a 5-hour window too, but the
plan pool is the number its UI shows and the week is the limit that bites.

The token is read from disk and sent to exactly one host, the one Codex sends
it to itself. `codex_poll.enabled` in config.json turns the endpoint off,
leaving only the logs.
"""

import datetime
import json
import os
import time
import urllib.error
import urllib.request

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

AUTH = os.path.join(HOME, "auth.json")
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# The endpoint is cheap and the number is slow: once a minute is plenty. A
# failure — no network, a token the CLI has not refreshed yet — backs off
# rather than asking a dead line every minute.
LIVE_INTERVAL = 60
LIVE_BACKOFF = 300
LIVE_TIMEOUT = 8

_cache = {"at": 0.0, "key": None, "reading": None}
_live = {"next": 0.0, "reading": None, "denied": False}


def forget():
    """Drop the caches, so the next read actually touches disk and network."""
    _cache.update({"at": 0.0, "key": None, "reading": None})
    _live.update({"next": 0.0, "reading": None, "denied": False})


def access_token(path=None):
    """The CLI's own token, or None. The CLI rewrites the file when it
    refreshes, so a later read recovers from an expired one."""
    try:
        with open(path or AUTH, encoding="utf-8") as f:
            tokens = (json.load(f) or {}).get("tokens") or {}
    except (OSError, ValueError, AttributeError):
        return None
    return tokens.get("access_token") or None


def parse_live(payload, now=None):
    """The endpoint's answer in the shape the log reader produces.

    The windows arrive as `primary_window` / `secondary_window` with the
    length in seconds; the weekly one is picked by that length, the same way
    as in a rollout, since which slot holds the week is not promised.
    """
    limits = (payload or {}).get("rate_limit") or {}
    windows = [w for w in (limits.get("primary_window"),
                           limits.get("secondary_window"))
               if isinstance(w, dict) and w.get("used_percent") is not None]
    if not windows:
        return None
    weekly = [w for w in windows
              if w.get("limit_window_seconds") == WEEK_MINUTES * 60]
    window = weekly[0] if weekly else max(
        windows, key=lambda w: w.get("limit_window_seconds") or 0)
    return {"used_percentage": float(window["used_percent"]),
            "resets_at": window.get("reset_at"),
            "window_minutes": (window.get("limit_window_seconds") or 0) // 60,
            "pool": MAIN_POOL,
            "plan_type": (payload or {}).get("plan_type"),
            "captured_at": now or time.time(),
            "source": "live"}


def fetch_live(token=None, timeout=LIVE_TIMEOUT, url=USAGE_URL, now=None):
    """Ask the endpoint the CLI uses. None on any failure: the logs are
    still there, and a widget is not the place to report a dead network."""
    token = token or access_token()
    if not token:
        return None
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "claudetg-bridge (stdlib urllib)",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # 401/403 is the one failure worth remembering: the token on disk
        # is no good, and no amount of retrying will change that. Anything
        # else is weather.
        _live["denied"] = e.code in (401, 403)
        return None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    _live["denied"] = False
    return parse_live(payload, now)


def auth_state(path=None):
    """Whether Codex is signed in: "ok", "expired" or "missing".

    Codex's token carries no expiry we can read, so "expired" is what the
    endpoint said rather than what the file says: a 401 means the token is
    there and no longer accepted.
    """
    if not access_token(path):
        return "missing"
    return "expired" if _live["denied"] else "ok"


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


def read(home=None, now=None, live=None):
    """The current Codex weekly limit, or None when there is nothing to show.

    Shaped like the windows in `usage`: `used_percentage` and `resets_at`, so
    the widget draws it with the same code as every other meter.

    The endpoint and the logs are both consulted, and the fresher reading
    wins: normally the endpoint, since a rollout only knows about requests
    made from this machine. With no network the last live answer keeps
    winning until a rollout has something newer to say, and with no token
    the logs are all there is. `live` defaults to on for the real home and
    off for any other — a test with a fixture directory must not reach
    for the real token.

    A window that turned over since it was written reads as zero with no
    reset time: the week it described has ended, nothing has been spent in
    the new one or there would be a newer record, and a dash where a number
    belongs reads as "broken", not as "fresh".
    """
    now = now or time.time()
    if live is None:
        live = home is None
    if live and now >= _live["next"]:
        found = fetch_live(now=now)
        if found:
            _live["reading"] = found
        _live["next"] = now + (LIVE_INTERVAL if found else LIVE_BACKOFF)
    return _fresher(_live["reading"] if live else None,
                    _from_logs(home, now), now)


def _fresher(live, logged, now):
    """Whichever reading was taken later, with the window-reset rule applied
    to it: the fresher of two readings is still the one to trust, even when
    it comes from a log rather than the wire."""
    picks = [r for r in (live, logged) if r]
    if not picks:
        return None
    reading = max(picks, key=lambda r: r.get("captured_at") or 0)
    resets = reading.get("resets_at") or 0
    if resets and resets <= now:
        return dict(reading, used_percentage=0.0, resets_at=None, reset=True)
    return reading


def _from_logs(home, now):
    """The newest reading in the rollouts, cached by what is on disk."""
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
        if captured and now - captured > MAX_AGE:
            reading = None                      # Codex has not run in weeks
    _cache["reading"] = reading
    return reading
