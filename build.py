#!/usr/bin/env python
"""Build the executables and the installer.

One command, one version number: whatever `claudetg/version.py` says is what
PyInstaller stamps and what Inno Setup names the setup file, so a release can
never ship a binary that disagrees with its own updater.

    python build.py             # executables + installer, signed
    python build.py --exe-only  # skip Inno Setup
    python build.py --no-sign   # leave everything unsigned

Signing is the default and a missing certificate is an error, not a warning
that scrolls past: an unsigned build looks exactly like a signed one from
here, and the one that got shipped was the one nobody noticed. The key is
not on this machine — see scripts/Sign-Windows.ps1.
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

SIGN_SCRIPT = os.path.join(ROOT, "scripts", "Sign-Windows.ps1")
# Ours. Everything under _internal is the Python runtime and the CRT, already
# signed by the PSF and by Microsoft — re-signing those would put our name on
# code we did not write.
OWN_EXES = ("ClaudeTelegram.exe", "claudetg-daemon.exe", "hookc.exe",
            "statusline.exe")


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


def powershell(*arguments, capture=False):
    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", SIGN_SCRIPT, *arguments]
    if capture:
        done = subprocess.run(command, cwd=ROOT, capture_output=True,
                              text=True)
        if done.returncode != 0:
            raise SystemExit((done.stderr or done.stdout).strip() or
                             f"{SIGN_SCRIPT} failed with {done.returncode}")
        return done.stdout.strip()
    return subprocess.run(command, cwd=ROOT).returncode


def sign(paths, what):
    print("==>", what)
    if powershell(*paths) != 0:
        raise SystemExit(f"{what} failed — nothing was shipped unsigned")


def inno_signing():
    """The two arguments Inno needs to sign the setup and the uninstaller.

    The command itself comes out of the signing script, so the tool path, the
    thumbprint and the timestamp URL are written down once.
    """
    return [f"/Sbonux={powershell('-InnoCommand', capture=True)}", "/DSign=1"]


def main():
    signing = "--no-sign" not in sys.argv
    print(f"building {__version__}{'' if signing else ' (unsigned)'}")
    stop_running()
    run([sys.executable, "-m", "PyInstaller", "claudetg.spec", "--noconfirm"],
        "PyInstaller")
    if signing:
        tree = os.path.join(ROOT, "dist", "ClaudeTelegram")
        # Before Inno packs them, or the installer would carry unsigned copies
        # of binaries that are signed on disk.
        sign([os.path.join(tree, name) for name in OWN_EXES],
             "signing the executables")
    if "--exe-only" in sys.argv:
        return

    run([iscc(), f"/DAppVersion={__version__}",
         *(inno_signing() if signing else []), "installer.iss"], "Inno Setup")
    setup = os.path.join(ROOT, "dist",
                         f"ClaudeTelegram-Setup-{__version__}.exe")
    if not os.path.exists(setup):
        # Newest, not first: dist\ holds every installer this project has
        # ever produced, and glob order would hand back the oldest of them.
        candidates = glob.glob(os.path.join(ROOT, "dist", "*Setup*.exe"))
        setup = max(candidates, key=os.path.getmtime) if candidates else None
    if signing:
        # Inno signed the setup and the uninstaller on its way out; this is
        # the check that it really did. Named explicitly rather than left to
        # the script's own defaults: dist\ keeps every installer ever built.
        print("==> verifying signatures")
        tree = os.path.join(ROOT, "dist", "ClaudeTelegram")
        built = [os.path.join(tree, name) for name in OWN_EXES]
        if setup:
            built.append(setup)
        if powershell("-VerifyOnly", *built) != 0:
            raise SystemExit("the build is not properly signed")
    if setup:
        digest = checksum(setup)
        size = os.path.getsize(setup) / (1024 * 1024)
        print(f"\n{os.path.basename(setup)}  {size:.1f} MB")
        print(f"{digest}  {os.path.basename(setup)}")
        print("\nPaste that checksum line into the GitHub release notes — the "
              "updater refuses to install a build it cannot verify.")
        if not signing:
            print("\nThis build is NOT signed. Windows will call it an "
                  "unknown publisher and Defender has quarantined an "
                  "unsigned widget before.")


if __name__ == "__main__":
    main()
