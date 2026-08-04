# -*- mode: python ; coding: utf-8 -*-
"""One folder, four executables, one copy of the runtime between them.

`hookc` runs on every single hook, so start-up cost is the design constraint:
a onefile build would unpack the whole runtime into a temp directory on each
invocation, in the middle of a session that is waiting for it. A one-folder
build starts immediately, and MERGE keeps the shared runtime in one place
instead of four.

Console flags matter as much:
* hookc and statusline talk over stdin/stdout — they must stay console apps;
* the daemon and the widget must not flash a console window at login.
"""

import os
import sys

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_submodules

DATA = [("lang", "lang"), ("config.example.json", ".")]
HIDDEN = collect_submodules("claudetg")


def analyse(script):
    return Analysis([script], pathex=[os.getcwd()], binaries=[], datas=DATA,
                    hiddenimports=HIDDEN, hookspath=[], runtime_hooks=[],
                    excludes=["pytest", "numpy", "PIL"], noarchive=False)


widget_a = analyse("widget.pyw")
daemon_a = analyse("run_daemon.py")
hookc_a = analyse("hookc.py")
status_a = analyse("statusline.py")

# Shared dependencies are pulled out of the followers and left in the leader.
MERGE((widget_a, "ClaudeTelegram", "ClaudeTelegram"),
      (daemon_a, "claudetg-daemon", "claudetg-daemon"),
      (hookc_a, "hookc", "hookc"),
      (status_a, "statusline", "statusline"))

widget_pyz = PYZ(widget_a.pure)
daemon_pyz = PYZ(daemon_a.pure)
hookc_pyz = PYZ(hookc_a.pure)
status_pyz = PYZ(status_a.pure)

ICON = "widget.ico" if os.path.exists("widget.ico") else None

sys.path.insert(0, os.getcwd())
from claudetg.version import __version__, parts  # noqa: E402


def version_resource(name, description):
    """Write a VS_VERSIONINFO for one executable and return its path.

    An unsigned PyInstaller build with no version resource at all is what
    Defender's heuristics look at, and Bearfoos.A!ml duly quarantined the
    widget — deleting the executable, both shortcuts and the uninstall entry.
    A signature is the real answer, but a binary that says who it is and what
    version it is costs nothing and is what every legitimate build carries.
    """
    numbers = (parts(__version__) + (0, 0, 0, 0))[:4]
    path = os.path.join("build", f"version_{name}.txt")
    os.makedirs("build", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers},
                    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
  kids=[
    StringFileInfo([StringTable("040904B0", [
      StringStruct("CompanyName", "bonuxq"),
      StringStruct("FileDescription", "{description}"),
      StringStruct("FileVersion", "{__version__}"),
      StringStruct("InternalName", "{name}"),
      StringStruct("LegalCopyright", "MIT License"),
      StringStruct("OriginalFilename", "{name}.exe"),
      StringStruct("ProductName", "Claude Telegram Bridge"),
      StringStruct("ProductVersion", "{__version__}"),
    ])]),
    VarFileInfo([VarStruct("Translation", [1033, 1200])]),
  ],
)
""")
    return path

widget_exe = EXE(widget_pyz, widget_a.scripts, [], exclude_binaries=True,
                 name="ClaudeTelegram", console=False, icon=ICON,
                 version=version_resource("ClaudeTelegram",
                                          "Claude Telegram Bridge"))
daemon_exe = EXE(daemon_pyz, daemon_a.scripts, [], exclude_binaries=True,
                 name="claudetg-daemon", console=False, icon=ICON,
                 version=version_resource("claudetg-daemon",
                                          "Claude Telegram Bridge daemon"))
# Console on purpose: Claude Code pipes JSON through these two.
hookc_exe = EXE(hookc_pyz, hookc_a.scripts, [], exclude_binaries=True,
                name="hookc", console=True, icon=ICON,
                version=version_resource("hookc", "Claude Code hook client"))
status_exe = EXE(status_pyz, status_a.scripts, [], exclude_binaries=True,
                 name="statusline", console=True, icon=ICON,
                 version=version_resource("statusline",
                                          "Claude Code status line"))

COLLECT(widget_exe, widget_a.binaries, widget_a.datas,
        daemon_exe, daemon_a.binaries, daemon_a.datas,
        hookc_exe, hookc_a.binaries, hookc_a.datas,
        status_exe, status_a.binaries, status_a.datas,
        strip=False, upx=False, name="ClaudeTelegram")
