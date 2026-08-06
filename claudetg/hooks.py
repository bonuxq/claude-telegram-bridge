"""Wiring the bridge into ~/.claude/settings.json.

Kept apart from install.py so the daemon can offer the same thing as a button:
on a fresh machine "run this command in a terminal" is exactly the step that
loses people.
"""

import json
import os
import shutil
import sys
import time

from .paths import app_dir, frozen

ROOT = app_dir()
SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
MARKER = "claudetg-hookc"

EVENTS = {
    "SessionStart": ["startup", "resume", "clear"],
    "SessionEnd": [None],
    "Stop": [None],
    "StopFailure": [None],
    "UserPromptSubmit": [None],
    "PreToolUse": ["AskUserQuestion"],
    "PostToolUse": ["TodoWrite"],
    # Once per batch of tool calls, before the next model request — the one
    # place a task typed mid-turn can reach the session without waiting for
    # the turn to end. Matcher-less by design: it is not about which tool ran.
    "PostToolBatch": [None],
    "PostToolUseFailure": [None],
    "Notification": [None],
}


def runner(script):
    """How to launch one of our entry points.

    Frozen builds ship an .exe per entry point and have no interpreter to
    call; from source it is the current interpreter plus the script path.
    """
    if frozen():
        return f'"{os.path.join(ROOT, script.replace(".py", ".exe"))}"'
    return f'"{sys.executable}" "{os.path.join(ROOT, script)}"'


def load_settings(path=None):
    path = path or SETTINGS
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data, path=None):
    path = path or SETTINGS
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def backup(path=None):
    path = path or SETTINGS
    if not os.path.exists(path):
        return None
    dest = f"{path}.backup-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, dest)
    return dest


def is_ours(entry):
    return any(MARKER in (h.get("command") or "") or "hookc" in (h.get("command") or "")
               for h in entry.get("hooks", []))


def strip_ours(settings):
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [e for e in hooks[event] if not is_ours(e)]
        if not hooks[event]:
            del hooks[event]
    return settings


def status(path=None):
    """What is wired up right now, for a screen that has to explain itself."""
    settings = load_settings(path)
    hooks = settings.get("hooks") or {}
    ours = sum(1 for entries in hooks.values() for e in entries if is_ours(e))
    line = json.dumps(settings.get("statusLine") or {})
    return {
        "hooks": ours,
        "expected": sum(len(m) for m in EVENTS.values()),
        "statusline": "statusline" in line,
        "statusline_foreign": bool(settings.get("statusLine")) and "statusline" not in line,
        "path": path or SETTINGS,
    }


def install(timeout=14400, path=None):
    """Idempotent: strips our previous entries before writing new ones."""
    settings = load_settings(path)
    saved = backup(path)
    strip_ours(settings)

    existing = settings.get("statusLine")
    if not existing or "statusline" in json.dumps(existing):
        settings["statusLine"] = {"type": "command",
                                  "command": runner("statusline.py"),
                                  "padding": 0}
        statusline = True
    else:
        statusline = False          # someone else owns it; leave it alone

    hooks = settings.setdefault("hooks", {})
    command = runner("hookc.py")
    for event, matchers in EVENTS.items():
        bucket = hooks.setdefault(event, [])
        for matcher in matchers:
            entry = {"hooks": [{"type": "command", "command": command,
                                "timeout": timeout}]}
            if matcher:
                entry["matcher"] = matcher
            bucket.append(entry)
    save_settings(settings, path)
    return {"ok": True, "backup": saved, "statusline": statusline,
            "hooks": sum(len(m) for m in EVENTS.values())}


def uninstall(path=None):
    if not os.path.exists(path or SETTINGS):
        return {"ok": True, "backup": None, "hooks": 0}
    saved = backup(path)
    settings = strip_ours(load_settings(path))
    if "statusline" in json.dumps(settings.get("statusLine") or {}):
        settings.pop("statusLine", None)
    save_settings(settings, path)
    return {"ok": True, "backup": saved, "hooks": 0}
