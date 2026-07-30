#!/usr/bin/env python
"""Entry point for the packaged daemon.

`python -m claudetg.daemon` has no equivalent in a frozen build — a build
needs a script to point at, so this is it. From source either one works.

It also carries the two installer chores, because the installer has no Python
of its own to call: wiring the hooks in, and taking them back out again.
"""

import os
import sys

ROOT = (os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from claudetg import hooks  # noqa: E402
from claudetg.daemon import main  # noqa: E402


def wire(enable):
    """Install or remove the Claude Code hooks, quietly enough for an
    installer: a failure here must not fail the whole installation."""
    try:
        result = hooks.install(3600) if enable else hooks.uninstall()
    except OSError as e:
        print(f"hooks: {e}", file=sys.stderr)
        return 0
    print(f"hooks {'installed' if enable else 'removed'}: "
          f"{result.get('hooks')} entries")
    return 0


if __name__ == "__main__":
    if "--install-hooks" in sys.argv:
        raise SystemExit(wire(True))
    if "--uninstall-hooks" in sys.argv:
        raise SystemExit(wire(False))
    main()
