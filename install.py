#!/usr/bin/env python
"""Command-line front end for the bridge's installation steps.

The same work is available as buttons in the widget's Telegram screen; this
exists for scripted installs and for machines without the widget.

Usage:
    python install.py                          # hooks + status line
    python install.py --timeout 3600           # hook timeout override
    python install.py --add-project PATH [--name NAME]
    python install.py --add-path PATH --for NAME   # extra dir for a project
    python install.py --autostart              # Windows: run at login
    python install.py --no-autostart
    python install.py --uninstall              # hooks and autostart both go
"""

import argparse
import json
import os
import sys

ROOT = (os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from claudetg import hooks  # noqa: E402
from claudetg.config import DEFAULTS  # noqa: E402

CONFIG = os.path.join(ROOT, "config.json")
WINDOWS = os.name == "nt"


def install(timeout):
    result = hooks.install(timeout)
    if result["backup"]:
        print(f"backup: {result['backup']}")
    if result["statusline"]:
        print("statusLine wired up (the only source of rate-limit data)")
    else:
        print("statusLine already points at another script — leaving it alone")
        print("  (without it there is no rate-limit data; wire statusline.py "
              "into your own status line to get it back)")
    print(f"installed {result['hooks']} hook entries (timeout={timeout}s) "
          f"into {hooks.SETTINGS}")


def uninstall():
    result = hooks.uninstall()
    if result["backup"]:
        print(f"backup: {result['backup']}")
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
    raise SystemExit(f"no project named '{project_name}'; known: {names}")


SHORTCUTS = {
    "Claude Telegram Widget.lnk": ("widget.pyw", None),
    "Claude Telegram Daemon.lnk": (None, "-m claudetg.daemon"),
}


def startup_dir():
    return os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup")


def autostart_supported():
    """Startup-folder shortcuts are a Windows concept. Everywhere else the
    daemon and hooks still work; only the login shortcuts are missing."""
    return WINDOWS and os.environ.get("APPDATA")


def autostart(enable=True, quiet=False):
    """Startup-folder shortcuts: user level, no admin rights, easy to inspect."""
    import subprocess
    if not autostart_supported():
        if not quiet:
            print("autostart: Windows only — start the daemon from your own "
                  "login items or a systemd/launchd unit")
        return
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


def pythonw():
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.exists(candidate) else sys.executable


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--add-project", help="bridge a project by path")
    p.add_argument("--add-path",
                   help="extra working directory to report under a project")
    p.add_argument("--for", dest="for_project",
                   help="project name that --add-path belongs to")
    p.add_argument("--name", help="display name for --add-project")
    p.add_argument("--timeout", type=int, default=None,
                   help="hook timeout in seconds (default: hook_timeout_seconds)")
    p.add_argument("--autostart", action="store_true",
                   help="run the daemon and widget at login (Windows)")
    p.add_argument("--no-autostart", action="store_true")
    args = p.parse_args()

    if args.add_project:
        return add_project(args.add_project, args.name)
    if args.add_path:
        if not args.for_project:
            raise SystemExit("--add-path needs --for <project name>")
        return add_path(args.add_path, args.for_project)
    if args.autostart:
        return autostart(True)
    if args.no_autostart:
        return autostart(False)
    if args.uninstall:
        autostart(False, quiet=True)   # nothing to remove off Windows
        return uninstall()

    timeout = args.timeout
    if timeout is None:
        try:
            with open(CONFIG, encoding="utf-8") as f:
                timeout = json.load(f).get("hook_timeout_seconds",
                                           DEFAULTS["hook_timeout_seconds"])
        except (OSError, ValueError):
            timeout = DEFAULTS["hook_timeout_seconds"]
    install(timeout)


if __name__ == "__main__":
    main()
