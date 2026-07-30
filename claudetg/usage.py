"""Rate-limit data: the cache written by statusline.py, its formatting, and
an optional direct poll for when the status line never runs.

The status line is the free, official source, but Claude Code only renders it
in its terminal UI — work through the VSCode extension and the cache is never
written at all. `fetch_live()` covers that gap by asking the same endpoint the
CLI itself uses, with the CLI's own OAuth token. It is opt-in: it spends a
credential the rest of the bridge does not need.
"""

import datetime
import json
import os
import time
import urllib.error
import urllib.request

from .i18n import t
from .paths import in_app

CACHE = in_app("usage.json")
CREDENTIALS = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

WINDOWS = ("five_hour", "seven_day")


def load(path=CACHE, max_age=None):
    """Return the cached payload, or None if missing or too old to trust."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not data.get("has_limits"):
        return None
    if max_age and time.time() - data.get("captured_at", 0) > max_age:
        return None
    return data


def access_token(path=None):
    """The CLI's own OAuth token. None when it is absent or already expired —
    the CLI rewrites this file when it refreshes, so a later read recovers."""
    try:
        with open(path or CREDENTIALS, encoding="utf-8") as f:
            oauth = (json.load(f) or {}).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return None
    expires = oauth.get("expiresAt")
    if expires and expires / 1000.0 <= time.time():
        return None
    return oauth.get("accessToken") or None


def epoch_of(stamp):
    """ISO-8601 with an offset -> unix seconds, the shape statusline.py caches."""
    if not stamp:
        return None
    try:
        return datetime.datetime.fromisoformat(str(stamp)).timestamp()
    except (ValueError, TypeError):
        return None


def windows_from(payload):
    """Map the endpoint's answer onto the status line's own vocabulary.

    Two shapes carry the same numbers: the flat `five_hour`/`seven_day`
    objects, and a `limits` list that additionally names per-model windows
    (`weekly_scoped` with `scope.model.display_name`). The list is where the
    Fable pool lives, and it is the only place it has ever been seen.
    """
    limits = {}
    for key in WINDOWS:
        window = payload.get(key) or {}
        used = window.get("utilization")
        if used is not None:
            limits[key] = {"used_percentage": float(used),
                           "resets_at": epoch_of(window.get("resets_at"))}
    for row in payload.get("limits") or []:
        percent, resets = row.get("percent"), epoch_of(row.get("resets_at"))
        if percent is None:
            continue
        entry = {"used_percentage": float(percent), "resets_at": resets}
        kind = row.get("kind")
        if kind == "session":
            limits.setdefault("five_hour", entry)
        elif kind == "weekly_all":
            limits.setdefault("seven_day", entry)
        elif kind == "weekly_scoped":
            model = ((row.get("scope") or {}).get("model") or {})
            name = (model.get("display_name") or "").strip().lower()
            if name:
                # Only the weekly window is scoped per model; the widget shows
                # a dash for the 5-hour one rather than inventing a number.
                limits.setdefault(name, {})["seven_day"] = entry
    return limits


def fetch_live(token=None, timeout=10, url=USAGE_URL):
    """Ask the endpoint the CLI uses. Returns a cache record, or None."""
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
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UsageError(str(e)[:200]) from None
    limits = windows_from(payload)
    if not limits:
        return None
    return {"captured_at": time.time(), "session_id": None,
            "rate_limits": limits, "has_limits": True, "source": "oauth"}


class UsageError(Exception):
    """The poll failed; the caller decides whether that is worth a message."""


def store(record, path=None):
    path = path or CACHE
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
    os.replace(tmp, path)


def status_line_fresh(seconds, path=None):
    """True when the status line itself wrote the cache recently.

    A record the poll stored does not count. Treating our own output as a live
    status line would stretch the real refresh rate out to the staleness
    window instead of the configured interval.
    """
    try:
        with open(path or CACHE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    if not data.get("has_limits") or data.get("source") == "oauth":
        return False
    return time.time() - data.get("captured_at", 0) <= seconds


def bar(percent, width=10):
    filled = int(round(width * max(0.0, min(100.0, percent)) / 100.0))
    return "█" * filled + "░" * (width - filled)


def when(epoch):
    """Absolute reset time plus how long that is from now."""
    if not epoch:
        return ""
    remaining = epoch - time.time()
    stamp = time.localtime(epoch)
    same_day = time.strftime("%Y%m%d", stamp) == time.strftime("%Y%m%d")
    clock = time.strftime("%H:%M", stamp)
    at = (t("usage.at_time", time=clock) if same_day
          else t("usage.at_date", date=time.strftime("%d.%m", stamp), time=clock))
    if remaining <= 0:
        return t("usage.reset", at=at)
    # Round, don't floor: flooring turns 59 seconds left into "in 0m".
    hours, minutes = divmod(int(round(remaining / 60)), 60)
    if hours >= 48:     # the weekly window: "93h" reads worse than days
        days, hours = divmod(hours, 24)
        left = t("usage.left.dh", d=days, h=hours)
    elif hours:
        left = t("usage.left.hm", h=hours, m=f"{minutes:02d}")
    else:
        left = t("usage.left.m", m=minutes)
    return t("usage.reset_in", at=at, left=left)


def reported_windows(limits):
    """(label key, window) for everything worth a line, scoped pools included.

    A per-model pool is a real limit like any other: leaving it out meant a
    spent Fable window was invisible everywhere except the widget.
    """
    for key in WINDOWS:
        yield key, limits.get(key) or {}
    for name, section in sorted(limits.items()):
        if name in WINDOWS or not isinstance(section, dict):
            continue
        window = section.get("seven_day")
        if isinstance(window, dict):
            yield name, window


def report(data):
    """Markdown block; empty string when there is nothing worth sending."""
    limits = (data or {}).get("rate_limits") or {}
    lines = []
    for key, window in reported_windows(limits):
        used = window.get("used_percentage")
        if used is None:
            continue
        # A scoped pool has no key of its own; its own name is the label.
        label = t("usage.window." + key) if key in WINDOWS else key.title()
        lines.append(f"`{bar(used)}` **{used:.0f}%** — {label}")
        moment = when(window.get("resets_at"))
        if moment:
            lines.append(f"   {moment}")
    return "\n".join(lines)
