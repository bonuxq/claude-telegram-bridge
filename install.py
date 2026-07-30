#!/usr/bin/env python
"""Wire the bridge into ~/.claude/settings.json (and back it up first).

Usage:
    python install.py                       # install hooks
    python install.py --add-project PATH [--name NAME]
    python install.py --uninstall
"""

import argparse
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
CONFIG = os.path.join(ROOT, "config.json")
MARKER = "claudetg-hookc"

EVENTS = {
    "SessionStart": ["startup", "resume", "clear"],
    "SessionEnd": [None],
    "Stop": [None],
    "StopFailure": [None],
    "UserPromptSubmit": [None],
    "PreToolUse": ["AskUserQuestion"],
    "PostToolUse": ["TodoWrite"],
    "PostToolUseFailure": [None],
    "Notification": [None],
}


def hook_command():
    # Absolute paths: hooks run with an unpredictable cwd.
    return f'"{sys.executable}" "{os.path.join(ROOT, "hookc.py")}"'


def backup(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = f"{path}.backup-{stamp}"
    shutil.copy2(path, dest)
    print(f"backup: {dest}")
    return dest


def load_settings():
    if not os.path.exists(SETTINGS):
        return {}
    with open(SETTINGS, encoding="utf-8") as f:
        return json.load(f)


def save_settings(data):
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS)


def is_ours(entry):
    return any(MARKER in (h.get("command") or "") or "hookc.py" in (h.get("command") or "")
               for h in entry.get("hooks", []))


def strip_ours(settings):
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [e for e in hooks[event] if not is_ours(e)]
        if not hooks[event]:
            del hooks[event]
    return settings


def install_statusline(settings):
    """The status line is the only place Claude Code exposes rate_limits."""
    existing = settings.get("statusLine")
    if existing and "statusline.py" not in json.dumps(existing):
        print("statusLine уже настроен другим скриптом — не трогаю его")
        return
    settings["statusLine"] = {
        "type": "command",
        "command": f'"{sys.executable}" "{os.path.join(ROOT, "statusline.py")}"',
        "padding": 0,
    }
    print("statusLine настроен (источник данных о лимитах)")


def install(timeout):
    settings = load_settings()
    if os.path.exists(SETTINGS):
        backup(SETTINGS)
    strip_ours(settings)          # idempotent: never stack duplicates
    install_statusline(settings)
    hooks = settings.setdefault("hooks", {})
    command = hook_command()
    for event, matchers in EVENTS.items():
        bucket = hooks.setdefault(event, [])
        for matcher in matchers:
            entry = {"hooks": [{"type": "command", "command": command,
                                "timeout": timeout}]}
            if matcher:
                entry["matcher"] = matcher
            bucket.append(entry)
    save_settings(settings)
    print(f"installed {sum(len(m) for m in EVENTS.values())} hook entries "
          f"(timeout={timeout}s) into {SETTINGS}")


def uninstall():
    if not os.path.exists(SETTINGS):
        return print("nothing to do")
    backup(SETTINGS)
    save_settings(strip_ours(load_settings()))
    print("bridge hooks removed")


def add_project(path, name):
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    key = os.path.abspath(path).replace("\\", "/")
    cfg.setdefault("projects", {})[key] = {"name": name or os.path.basename(key)}
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"project registered: {key} -> {cfg['projects'][key]['name']}")


def add_path(extra, project_name):
    """Bind another working directory to an existing project's topic."""
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    extra = os.path.abspath(extra).replace("\\", "/")
    for key, meta in cfg.get("projects", {}).items():
        if (meta.get("name") or "").lower() == project_name.lower():
            paths = meta.setdefault("extra_paths", [])
            if extra not in paths:
                paths.append(extra)
            with open(CONFIG, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            return print(f"path bound: {extra} -> {meta['name']}")
    names = [m.get("name") for m in cfg.get("projects", {}).values()]
    raise SystemExit(f"проект '{project_name}' не найден; есть: {names}")


SHORTCUTS = {
    "Claude Telegram Widget.lnk": ("widget.pyw", None),
    "Claude Telegram Daemon.lnk": (None, "-m claudetg.daemon"),
}


def startup_dir():
    return os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup")


def pythonw():
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.exists(candidate) else sys.executable


def autostart(enable=True):
    """Startup-folder shortcuts: user level, no admin rights, easy to inspect."""
    import subprocess
    target = startup_dir()
    for filename, (script, module) in SHORTCUTS.items():
        path = os.path.join(target, filename)
        if not enable:
            if os.path.exists(path):
                os.remove(path)
                print(f"removed {path}")
            continue
        args = f'"{os.path.join(ROOT, script)}"' if script else module
        ps = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
            "$s.TargetPath = '{exe}';"
            "$s.Arguments = '{args}';"
            "$s.WorkingDirectory = '{cwd}';"
            "$s.WindowStyle = 7;"
            "$s.Save()"
        ).format(lnk=path.replace("'", "''"), exe=pythonw(),
                 args=args.replace("'", "''"), cwd=ROOT)
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                       capture_output=True)
        print(f"autostart: {path}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--add-project")
    p.add_argument("--add-path", help="дополнительный рабочий каталог")
    p.add_argument("--for", dest="for_project", help="имя проекта для --add-path")
    p.add_argument("--name")
    p.add_argument("--timeout", type=int, default=None)
    p.add_argument("--autostart", action="store_true")
    p.add_argument("--no-autostart", action="store_true")
    args = p.parse_args()

    if args.add_project:
        return add_project(args.add_project, args.name)
    if args.add_path:
        if not args.for_project:
            raise SystemExit("--add-path требует --for <имя проекта>")
        return add_path(args.add_path, args.for_project)
    if args.autostart:
        return autostart(True)
    if args.no_autostart:
        return autostart(False)
    if args.uninstall:
        autostart(False)
        return uninstall()

    timeout = args.timeout
    if timeout is None:
        with open(CONFIG, encoding="utf-8") as f:
            timeout = json.load(f).get("hook_timeout_seconds", 3600)
    install(timeout)


if __name__ == "__main__":
    main()
