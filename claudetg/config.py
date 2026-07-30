"""Config loading. Plain JSON so that nothing outside the stdlib is needed."""

import json
import os
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state.json")

DEFAULTS = {
    "bot_token": "",
    "chat_id": None,
    "host": "127.0.0.1",
    "port": 8787,
    # Must stay below the hook `timeout` in settings.json, so the hook returns
    # an answer of its own accord instead of being killed mid-wait.
    "wait_seconds": 3300,
    "hook_timeout_seconds": 3600,
    # Absolute project paths with custom names / extra_paths. With
    # auto_discover on these are optional overrides, not a whitelist.
    "projects": {},
    # Off by default: projects are chosen from a list (widget or /projects),
    # so nothing leaves the machine without being picked. Turn on to bridge
    # every session automatically.
    "auto_discover": False,
    # Never bridged, even with auto_discover on. Prefix match.
    "exclude_paths": [],
    "spawn": {"enabled": False, "permission_mode": "default"},
    "away_default": False,
    # Which events reach Telegram while you are AT the PC. Away mode sends
    # everything. Kinds: stop, todo, failure, session, live (the streaming
    # assistant text), usage (limits after each turn), notification.
    # Defaults preserve the original behaviour: live/usage/notification flow
    # even at the PC, just silently.
    "log_when_present": ["live", "notification", "session", "stop", "todo", "usage"],
    # Per-tool failure reports (❌ Bash/Read with the error dump).
    "report_tool_failures": False,
    "status_monitor": {"enabled": True, "interval_seconds": 300, "components": []},
    # Alert when a turn has been running this long without finishing.
    "watchdog": {"enabled": True, "minutes": 20},
    "daily_digest": {"enabled": True, "hour": 21},
    "git_summary": True,
    # Send each assistant text block as soon as it lands in the transcript,
    # instead of one batch at the end of the turn.
    "live_messages": True,
    # Rate-limit report after each turn; data comes from statusline.py.
    "usage_report": {"enabled": True, "max_age_seconds": 3600},
}

_lock = threading.Lock()


def load(path=PATH):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    cfg["projects"] = {normalize(k): v for k, v in cfg.get("projects", {}).items()}
    return cfg


def save(cfg, path=PATH):
    with _lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def normalize(path):
    """Case- and separator-insensitive key for Windows paths."""
    return os.path.normcase(os.path.abspath(path)).replace("\\", "/")


def roots_of(root, meta):
    """A project's own path plus any extra working directories bound to it.

    Claude often runs in sibling trees that belong to the same project (test
    data, a game client, a reference checkout); those must land in the same
    Telegram topic, not spawn one of their own.
    """
    yield root
    for extra in (meta or {}).get("extra_paths") or []:
        yield normalize(extra)


def project_for(cfg, cwd):
    """Longest-prefix match, so subdirectories of a project still resolve."""
    key = normalize(cwd)
    best = None
    for root, meta in cfg["projects"].items():
        for candidate in roots_of(root, meta):
            if key == candidate or key.startswith(candidate.rstrip("/") + "/"):
                if best is None or len(candidate) > best[0]:
                    best = (len(candidate), root, meta)
    return (best[1], best[2]) if best else None


def is_excluded(cfg, *paths):
    for excluded in cfg.get("exclude_paths") or []:
        root = normalize(excluded).rstrip("/")
        for path in paths:
            if not path:
                continue
            key = normalize(path)
            if key == root or key.startswith(root + "/"):
                return True
    return False


def transcript_project_key(transcript_path):
    """Claude Code already groups sessions by project: one directory under
    ~/.claude/projects per project. That directory is a stable id even when
    the session wanders into subfolders."""
    if not transcript_path:
        return None
    parent = os.path.dirname(os.path.abspath(transcript_path))
    return normalize(parent) if parent else None


def project_root_from_transcript(transcript_path):
    """The session's original cwd, read from the transcript's first entry.

    Decoding the encoded directory name is not safe: separators and hyphens
    are ambiguous (`Convert_lic30-lic40` proves it).
    """
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                cwd = entry.get("cwd")
                if cwd:
                    return cwd
    except OSError:
        pass
    return None


def load_state(path=STATE_PATH):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {}


def save_state(state, path=STATE_PATH):
    with _lock:
        # Unique temp name: os.replace on Windows fails if anything else holds
        # the file, and a shared temp name turns that into a lost write.
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    os.unlink(tmp)
                    raise
                time.sleep(0.1)
