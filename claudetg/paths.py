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
