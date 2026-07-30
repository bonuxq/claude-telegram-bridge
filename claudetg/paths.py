"""Where the bridge's files live, from source and from a frozen build.

Two different roots, and conflating them is the classic packaging bug:

* `app_dir()` — the installation itself: config.json, state.json, usage.json,
  the logs, the icons. Writable, survives restarts, sits next to the
  executable so a user can find and edit it.
* `resources()` — read-only data shipped inside the build (the language
  bundles). PyInstaller unpacks these into a temporary directory that is
  deleted on exit; anything written there is silently lost.

From source both are the project root, which is why the difference only
shows up once the build exists.
"""

import os
import sys


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resources():
    return getattr(sys, "_MEIPASS", None) or app_dir()


def in_app(*parts):
    return os.path.join(app_dir(), *parts)


def in_resources(*parts):
    return os.path.join(resources(), *parts)


def frozen():
    return bool(getattr(sys, "frozen", False))


def single_instance(name):
    """Claim a per-user name, or report that someone else already holds it.

    Returns the handle on success and None when another copy is running. The
    handle must stay referenced for as long as the process lives — dropping it
    releases the claim.

    A named mutex rather than a lock file: Windows drops it when the process
    dies, however it dies, so a crash cannot leave a stale claim that keeps
    the app from ever starting again. "Local\\" scopes it to the session, so
    two users on one machine are not blocked by each other.
    """
    if os.name != "nt":
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\" + name)
    if not handle:
        return True                     # cannot tell: let the app start
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return handle
