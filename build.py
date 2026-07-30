#!/usr/bin/env python
"""Build the executables and the installer.

One command, one version number: whatever `claudetg/version.py` says is what
PyInstaller stamps and what Inno Setup names the setup file, so a release can
never ship a binary that disagrees with its own updater.

    python build.py             # executables + installer
    python build.py --exe-only  # skip Inno Setup
"""

import glob
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from claudetg.version import __version__  # noqa: E402

ISCC_CANDIDATES = (
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
)


def run(command, what):
    print("==>", what)
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"{what} failed with {result.returncode}")


def stop_running():
    """The build overwrites files a running copy holds open."""
    if os.name != "nt":
        return
    for name in ("claudetg-daemon.exe", "ClaudeTelegram.exe", "hookc.exe",
                 "statusline.exe"):
        subprocess.run(["taskkill", "/f", "/im", name],
                       capture_output=True)


def iscc():
    for path in ISCC_CANDIDATES:
        if os.path.exists(path):
            return path
    found = shutil.which("iscc")
    if found:
        return found
    raise SystemExit("Inno Setup not found — install it or pass --exe-only")


def checksum(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    print(f"building {__version__}")
    stop_running()
    run([sys.executable, "-m", "PyInstaller", "claudetg.spec", "--noconfirm"],
        "PyInstaller")
    if "--exe-only" in sys.argv:
        return

    run([iscc(), f"/DAppVersion={__version__}", "installer.iss"], "Inno Setup")
    setup = os.path.join(ROOT, "dist",
                         f"ClaudeTelegram-Setup-{__version__}.exe")
    if not os.path.exists(setup):
        candidates = glob.glob(os.path.join(ROOT, "dist", "*Setup*.exe"))
        setup = candidates[0] if candidates else None
    if setup:
        digest = checksum(setup)
        size = os.path.getsize(setup) / (1024 * 1024)
        print(f"\n{os.path.basename(setup)}  {size:.1f} MB")
        print(f"{digest}  {os.path.basename(setup)}")
        print("\nPaste that checksum line into the GitHub release notes — the "
              "updater refuses to install a build it cannot verify.")


if __name__ == "__main__":
    main()
