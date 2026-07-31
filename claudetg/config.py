"""Config loading. Plain JSON so that nothing outside the stdlib is needed."""

import json
import os
import threading
import time

from .paths import app_dir

ROOT = app_dir()
PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state.json")

DEFAULTS = {
    "bot_token": "",
    "chat_id": None,
    # UI language for Telegram messages and the widget: "en", "ru", "uk",
    # "ko", "zh", "vi", "el", "pt", "es". None = auto-detect from the OS.
    "language": None,
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
    # Run a headless `claude -p` when a task arrives for a project that has no
    # live session. This is remote code execution on this machine driven by a
    # chat message: off unless deliberately enabled.
    "spawn": {
        "enabled": False,
        # One of the CLI's own modes: acceptEdits, auto, bypassPermissions,
        # manual, dontAsk, plan. Anything else (including the historic
        # "default") leaves the flag off and lets Claude Code decide.
        "permission_mode": "acceptEdits",
        # None keeps the CLI's configured model.
        "model": None,
        # Backstop against an orphan: away mode lets the agent's own Stop hook
        # hold it for wait_seconds on top of the actual work. 0 = no limit.
        "timeout_seconds": 7200,
    },
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
    # Per-day tallies feed the digest and are rewritten into state.json on
    # every turn; without a bound the file grows for the life of the install.
    "stats_retention_days": 14,
    "git_summary": True,
    # Send each assistant text block as soon as it lands in the transcript,
    # instead of one batch at the end of the turn.
    "live_messages": True,
    # Rate-limit report after each turn; data comes from statusline.py.
    "usage_report": {"enabled": True, "max_age_seconds": 3600},
    # Where those limits actually come from for most installs. Claude Code
    # renders the status line only in its terminal UI, so a VSCode-only setup
    # never writes the cache and the meters would sit empty forever — which is
    # exactly what a fresh install looked like. So this is on: the daemon asks
    # the same endpoint the CLI uses, with the CLI's own OAuth token from
    # ~/.claude/.credentials.json. It reads a credential, but sends it nowhere
    # except Anthropic, and only ever asks for your own usage.
    "usage_poll": {"enabled": True, "interval_seconds": 60,
                   "stale_after_seconds": 45, "timeout_seconds": 10},
    # Self-update from GitHub Releases. Frozen builds only — from source the
    # update is `git pull`. Installs itself the first moment no session is
    # running, waiting or being spawned.
    "updates": {"enabled": True, "interval_hours": 6},
}

_lock = threading.Lock()


def load(path=None):
    # Resolved per call, never bound as a default argument: a default is
    # evaluated at import time, so pointing PATH somewhere else afterwards
    # (the test suite does) would silently keep writing the installed file.
    path = path or PATH
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    cfg["projects"] = {normalize(k): v for k, v in cfg.get("projects", {}).items()}
    return cfg


def save(cfg, path=None):
    path = path or PATH
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


def load_state(path=None):
    path = path or STATE_PATH
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {}


def save_state(state, path=None):
    path = path or STATE_PATH
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
