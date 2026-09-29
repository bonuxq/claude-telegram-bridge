"""Dev harness: run the card against canned data, in a scratch profile.

The live widget reads config.json / widget.json next to itself and polls the
installed daemon. That makes it useless for looking at a state on purpose —
the away theme, a spent week, the alarm row — so this points the module at a
throwaway profile and feeds it a fixed snapshot.

    python tools/preview.py <state> [scale] [language]

States: atpc, away, dead, monitor (Telegram off), alarm, warm, session and
hot (windows filling up and spent), locked (nobody signed in), empty (no
vendor installed), docs (what docs/widget.png is rendered from).

Stdlib only, like everything else here: it imports the widget by path,
points its two files at tools/.preview/ and hands it a fixed answer in place
of the daemon, the usage cache and the Codex reader.
"""

import ctypes
import importlib.util
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRATCH = os.path.join(HERE, ".preview")
sys.path.insert(0, ROOT)


def close_running():
    """Drop whatever copy is on screen, so a re-run just replaces it.

    The widget's own single-instance guard would send this one away instead,
    and looking at a change means looking at the new card, not the old one.
    """
    user32 = ctypes.windll.user32
    for title in ("Claude ↔ Telegram", "Claude ↔ Telegram control"):
        while True:
            hwnd = user32.FindWindowW(None, title)
            if not hwnd:
                break
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            subprocess.run(["taskkill", "/PID", str(pid.value), "/F"],
                           capture_output=True)
            time.sleep(0.4)


def load_widget():
    spec = importlib.util.spec_from_file_location(
        "widget_preview", os.path.join(ROOT, "widget.pyw"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["widget_preview"] = module
    spec.loader.exec_module(module)
    return module


STATES = {
    "atpc":    dict(away=False, telegram=True),
    "away":    dict(away=True, telegram=True),
    "dead":    dict(dead=True, telegram=True),
    "monitor": dict(away=False, telegram=False),
    "alarm":   dict(away=True, telegram=True,
                    alarm={"enabled": True, "minutes": 15}),
    "hot":     dict(away=False, telegram=True,
                    five_hour=94.0, seven_day=97.0, fable=91.0, codex=99.0),
    "warm":    dict(away=False, telegram=True,
                    five_hour=72.0, seven_day=64.0, fable=38.0, codex=91.0),
    "session": dict(away=False, telegram=True,
                    five_hour=93.0, seven_day=41.0, fable=12.0, codex=58.0),
    "locked":  dict(away=False, telegram=True, auth="missing"),
    # What docs/widget.png is rendered from: an ordinary working card.
    "docs":    dict(away=False, telegram=True,
                    five_hour=24.0, seven_day=56.0, fable=18.0, codex=63.0),
    "empty":   dict(away=False, telegram=True, claude=False, codex=None),
}


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "monitor"
    scale = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    language = sys.argv[3] if len(sys.argv) > 3 else "uk"
    state = STATES[name]

    close_running()
    os.makedirs(SCRATCH, exist_ok=True)
    config = os.path.join(SCRATCH, "config.json")
    with open(config, "w", encoding="utf-8") as f:
        json.dump({"language": language, "port": 65000}, f)
    pos = os.path.join(SCRATCH, "widget.json")
    with open(pos, "w", encoding="utf-8") as f:
        json.dump({"x": 60, "y": 120, "alpha": 1.0, "scale": scale,
                   "fable_shown": 1, "claude_shown": 1, "codex_shown": 1,
                   "auto_away_seconds": 0, "click_through": 0}, f)

    w = load_widget()
    w.CONFIG, w.POS_FILE = config, pos
    now = time.time()

    snapshot = None if state.get("dead") else {
        "away": state["away"], "telegram": state["telegram"],
        "sessions": ["claude-telegram-bridge", "Lineage2JS-stable"],
        "waiting": [], "queued": 0, "alarm": state.get("alarm") or {},
    }
    w.Widget.request = lambda self, path, payload=None: snapshot

    limits = {"captured_at": now, "has_limits": True, "rate_limits": {
        "five_hour": {"used_percentage": state.get("five_hour", 8.0),
                      "resets_at": now + 9000},
        "seven_day": {"used_percentage": state.get("seven_day", 32.0),
                      "resets_at": now + 4 * 86400},
        "fable": {"seven_day": {"used_percentage": state.get("fable", 0.0),
                                "resets_at": now + 4 * 86400}}}}
    if not state.get("claude", True):
        limits = {}
    w.usage.load = lambda *a, **k: limits
    w.usage.expire_spent_windows = lambda data, now=None: data
    w.usage.present = lambda *a, **k: state.get("claude", True)
    w.usage.auth_state = lambda *a, **k: state.get("auth", "ok")

    reading = None if "codex" in state and state["codex"] is None else {
        "used_percentage": state.get("codex", 91.0), "resets_at": now + 4 * 86400,
        "window_minutes": 10080, "pool": "codex", "captured_at": now,
        "source": "live"}
    w.codex.read = lambda *a, **k: reading
    w.codex.auth_state = lambda *a, **k: state.get("auth", "ok")

    # A scratch profile has no bot token, so the setup screen would open
    # itself over the very card this is here to look at.
    w.Widget.offer_setup = lambda self: None

    card = w.Widget()
    card.root.title("Claude ↔ Telegram")
    card.run()


if __name__ == "__main__":
    main()
