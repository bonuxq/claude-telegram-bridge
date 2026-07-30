"""Formatting for the rate-limit report captured by statusline.py."""

import json
import os
import time

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "usage.json")

WINDOWS = (("five_hour", "5-часовое окно"), ("seven_day", "Недельное"))


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
    at = time.strftime("в %H:%M" if same_day else "%d.%m в %H:%M", stamp)
    if remaining <= 0:
        return f"обнуление {at}"
    # Round, don't floor: flooring turns 59 seconds left into "через 0м".
    hours, minutes = divmod(int(round(remaining / 60)), 60)
    if hours >= 48:     # the weekly window: "93ч" reads worse than days
        days, hours = divmod(hours, 24)
        left = f"{days}д {hours}ч"
    elif hours:
        left = f"{hours}ч {minutes:02d}м"
    else:
        left = f"{minutes}м"
    return f"обнуление {at} (через {left})"


def report(data):
    """Markdown block; empty string when there is nothing worth sending."""
    limits = (data or {}).get("rate_limits") or {}
    lines = []
    for key, label in WINDOWS:
        window = limits.get(key) or {}
        used = window.get("used_percentage")
        if used is None:
            continue
        lines.append(f"`{bar(used)}` **{used:.0f}%** — {label}")
        moment = when(window.get("resets_at"))
        if moment:
            lines.append(f"   {moment}")
    return "\n".join(lines)
