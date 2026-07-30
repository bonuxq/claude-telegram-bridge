"""List the projects Claude Code already knows about, for the picker.

Claude Code keeps one directory per project under ~/.claude/projects. The
directory name is an encoded path whose separators are ambiguous, so the real
root is read from a transcript instead of decoded from the name.
"""

import os

from . import config as cfgmod
from .i18n import t

CLAUDE_HOME = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def _newest_transcript(directory):
    best, best_mtime = None, -1
    try:
        for entry in os.scandir(directory):
            if not entry.name.endswith(".jsonl"):
                continue
            mtime = entry.stat().st_mtime
            if mtime > best_mtime:
                best, best_mtime = entry.path, mtime
    except OSError:
        return None, 0
    return best, best_mtime


def list_projects(home=CLAUDE_HOME, cfg=None):
    """Return project descriptors, most recently used first."""
    cfg = cfg or {}
    selected = {cfgmod.normalize(k): v for k, v in (cfg.get("projects") or {}).items()}
    found = []
    try:
        directories = [e.path for e in os.scandir(home) if e.is_dir()]
    except OSError:
        directories = []

    for directory in directories:
        transcript, mtime = _newest_transcript(directory)
        if not transcript:
            continue
        root = cfgmod.project_root_from_transcript(transcript)
        if not root:
            continue
        key = cfgmod.normalize(root)
        meta = selected.get(key)
        found.append({
            "root": root.replace("\\", "/"),
            "key": key,
            "name": (meta or {}).get("name") or os.path.basename(
                str(root).rstrip("\\/")) or t("project.fallback"),
            "enabled": meta is not None,
            "last_active": mtime,
            "exists": os.path.isdir(root),
        })

    # Projects registered by hand may not appear under ~/.claude/projects yet.
    for key, meta in selected.items():
        if any(p["key"] == key for p in found):
            continue
        found.append({
            "root": key, "key": key,
            "name": meta.get("name") or os.path.basename(key.rstrip("/")),
            "enabled": True, "last_active": 0, "exists": os.path.isdir(key),
        })

    found.sort(key=lambda p: (-p["last_active"], p["name"].lower()))
    return found


def apply_selection(cfg, keys):
    """Rewrite cfg['projects'] to exactly the chosen set, keeping extras."""
    chosen = {cfgmod.normalize(k) for k in keys}
    catalogue = {p["key"]: p for p in list_projects(cfg=cfg)}
    current = {cfgmod.normalize(k): v for k, v in (cfg.get("projects") or {}).items()}

    projects = {}
    for key in chosen:
        meta = current.get(key) or {}
        entry = {"name": meta.get("name")
                 or (catalogue.get(key) or {}).get("name")
                 or os.path.basename(key.rstrip("/"))}
        # Never silently drop hand-tuned extra paths when toggling a project.
        if meta.get("extra_paths"):
            entry["extra_paths"] = meta["extra_paths"]
        source = catalogue.get(key)
        projects[source["root"] if source else key] = entry
    cfg["projects"] = projects
    return projects
