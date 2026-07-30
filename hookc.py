#!/usr/bin/env python
"""Thin hook client: stdin -> daemon -> stdout.

Runs on every hook invocation, so it stays stdlib-only and does as little as
possible. Every failure path exits 0 with no output: a broken bridge must never
be able to block or corrupt a Claude Code session.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.json")


def bail():
    sys.exit(0)


def trace(cfg, *parts):
    """Opt-in record of every hook invocation. Without it, diagnosing a quiet
    bridge is guesswork: the client is designed to fail silently."""
    if not (cfg or {}).get("debug_hooks"):
        return
    try:
        with open(os.path.join(ROOT, "hooks.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} " +
                    " ".join(str(p) for p in parts) + "\n")
    except OSError:
        pass


def main():
    try:
        # Explicit UTF-8: Claude Code writes UTF-8, but Python on Windows
        # defaults stdin to the ANSI codepage and would mangle any non-ASCII.
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        bail()

    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        bail()

    event = data.get("hook_event_name")
    cwd = data.get("cwd") or ""
    if not in_scope(cfg, cwd, data.get("transcript_path")):
        trace(cfg, event, "SKIP (вне списка)", cwd)
        bail()
    trace(cfg, event, "->", cwd)

    url = f"http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 8787)}/event"
    # Outlive the daemon's own wait, or the hook would abandon a live question.
    timeout = int(cfg.get("wait_seconds", 3300)) + 120

    result = post(url, data, timeout)
    if result is None and cfg.get("autostart", True):
        trace(cfg, event, "демон не ответил, поднимаю")
        if start_daemon(cfg):
            result = post(url, data, timeout)
    trace(cfg, event, "ответ демона:", json.dumps(result, ensure_ascii=False)[:200])
    if not result:
        bail()

    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()
    sys.exit(0)


def in_scope(cfg, cwd, transcript=None):
    """Cheap gate so hooks cost almost nothing where the bridge is unwanted.

    Mirrors the daemon's resolution, including `extra_paths` and
    `exclude_paths`; the daemon decides the rest.
    """
    for excluded in cfg.get("exclude_paths") or []:
        root = norm(excluded).rstrip("/")
        for path in (cwd, transcript):
            if path and (norm(path) == root or norm(path).startswith(root + "/")):
                return False

    # Default must match config.DEFAULTS: the daemon would reject these
    # anyway, but every unbridged project would pay for a pointless round trip.
    if cfg.get("auto_discover", False):
        return True

    if not cwd:
        return False
    key = norm(cwd)
    for root, meta in cfg.get("projects", {}).items():
        candidates = [root] + list((meta or {}).get("extra_paths") or [])
        for candidate in candidates:
            candidate = norm(candidate)
            if key == candidate or key.startswith(candidate.rstrip("/") + "/"):
                return True
    return False


def norm(path):
    return os.path.normcase(os.path.abspath(path)).replace("\\", "/")


def post(url, payload, timeout):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def start_daemon(cfg):
    """Bring the daemon up in the background and wait for it to answer."""
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen(
            [sys.executable, "-m", "claudetg.daemon"],
            cwd=ROOT, creationflags=creation,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        return False
    health = f"http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 8787)}/"
    for _ in range(20):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(health, timeout=2):
                return True
        except (urllib.error.URLError, OSError):
            continue
    return False


if __name__ == "__main__":
    main()
