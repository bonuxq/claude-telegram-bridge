#!/usr/bin/env python
"""Status line that doubles as the source of rate-limit data.

Claude Code hands the status line a JSON blob containing `rate_limits`
(5-hour and 7-day windows with `used_percentage` and `resets_at`). Hooks get
no such field, so this script caches it for the bridge and prints a normal
status line for VSCode.

Runs on every render, so it does nothing but a small file write.
"""

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "usage.json")
RAW_SAMPLE = os.path.join(ROOT, "usage_sample.json")


def read_payload():
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        return {}


def cache(payload):
    limits = payload.get("rate_limits") or {}
    record = {
        "captured_at": time.time(),
        "session_id": payload.get("session_id"),
        "rate_limits": limits,
        # Kept so a missing rate_limits can be told apart from a stale cache.
        "has_limits": bool(limits),
    }
    try:
        tmp = f"{CACHE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp, CACHE)
    except OSError:
        pass
    if not os.path.exists(RAW_SAMPLE):
        try:  # one raw sample, for diagnosing what this version actually sends
            with open(RAW_SAMPLE, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


def bar(percent, width=10):
    filled = int(round(width * max(0.0, min(100.0, percent)) / 100.0))
    return "█" * filled + "░" * (width - filled)


def line(payload):
    parts = []
    model = (payload.get("model") or {}).get("display_name")
    if model:
        parts.append(model)
    workspace = payload.get("workspace") or {}
    directory = workspace.get("current_dir") or payload.get("cwd")
    if directory:
        parts.append(os.path.basename(str(directory).rstrip("\\/")))

    limits = payload.get("rate_limits") or {}
    for label, window in (("5ч", "five_hour"), ("нед", "seven_day")):
        data = limits.get(window) or {}
        used = data.get("used_percentage")
        if used is None:
            continue
        chunk = f"{label} {bar(used)} {used:.0f}%"
        resets = data.get("resets_at")
        if resets:
            chunk += f" →{time.strftime('%H:%M', time.localtime(resets))}"
        parts.append(chunk)
    return "  ".join(parts) or "claude"


def main():
    payload = read_payload()
    cache(payload)
    sys.stdout.buffer.write(line(payload).encode("utf-8"))


if __name__ == "__main__":
    main()
