"""Formatting for the rate-limit report captured by statusline.py."""

import json
import os
import time

from .i18n import t

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "usage.json")

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


def report(data):
    """Markdown block; empty string when there is nothing worth sending."""
    limits = (data or {}).get("rate_limits") or {}
    lines = []
    for key in WINDOWS:
        window = limits.get(key) or {}
        used = window.get("used_percentage")
        if used is None:
            continue
        lines.append(f"`{bar(used)}` **{used:.0f}%** — {t('usage.window.' + key)}")
        moment = when(window.get("resets_at"))
        if moment:
            lines.append(f"   {moment}")
    return "\n".join(lines)
