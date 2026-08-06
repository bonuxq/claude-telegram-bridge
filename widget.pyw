#!/usr/bin/env pythonw
"""Desktop toggle for the bridge: a borderless always-on-top card.

The capsule button switches presence: at the PC hooks only log to Telegram,
away they block and wait for answers in the chat. The meters show the
rate-limit windows from usage.json — the same cache statusline.py feeds the
Telegram reports from.

No OS frame anywhere: the card, the popup menu and the Projects/Features
windows are all drawn by hand (tk.Menu on Windows paints a system border
that cannot be themed). Drag to move, right-click or the tray icon for the
menu. Tkinter + ctypes only, so the zero-dependency rule still holds.
"""

import ctypes
import ctypes.wintypes      # a submodule: plain `import ctypes` does not bring it
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from tkinter import font as tkfont

ROOT = (os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from claudetg import i18n, paths, usage  # noqa: E402  (needs ROOT on the path)
from claudetg.i18n import t  # noqa: E402

CONFIG = os.path.join(ROOT, "config.json")
POS_FILE = os.path.join(ROOT, "widget.json")
POLL_MS = 3000

# Dark chart chrome (validated reference palette: dataviz skill).
SURFACE = "#1a1a19"        # window background
RAISED = "#262624"         # buttons, the toggle capsule
RAISED_HI = "#31312e"      # hover
PRIMARY = "#ffffff"        # values, the toggle caption
SECONDARY = "#c3c2b7"      # labels
MUTED = "#898781"          # reset times, hints
HAIRLINE = "#2c2c2a"       # separators
EDGE = "#3a3a37"           # window outline (replaces the OS frame)

# Status steps (fixed palette; all clear 3:1 on the dark surface).
GOOD = "#0ca30c"
WARNING = "#fab219"
CRITICAL = "#d03b3b"
ACCENT = "#3987e5"         # meter fill while usage is comfortable
SEG_COLORS = {"standard": "#86b6ef",   # light blue (sequential ramp, step 250)
              "fable": "#d95926"}      # orange (categorical slot 2, dark step)
# A nearly spent window turns the whole card, not just the meter: the widget
# is small and usually parked behind something, and a red card still reads.
# Dim for the 5-hour window, which refills by itself; full red for the weekly
# one, which does not.
ALERT_PERCENT = 90
ALERT_SESSION = "#3b201f"
ALERT_WEEK = "#752b2a"

# The 5-hour window is the one that bites during a working session, so it gets
# a finer ramp than the plain calm/warn/critical the other meters use: the
# colour starts moving at 60% instead of jumping at 70.
SESSION_STEPS = ((90, CRITICAL), (80, "#e5711c"), (70, "#f59122"), (60, WARNING))

# Themes hold i18n keys: the language is only known once config.json is read.
AT_PC = {"dot": GOOD, "button": "widget.atpc", "label": "widget.atpc.hint"}
AWAY = {"dot": WARNING, "button": "widget.away", "label": "widget.away.hint"}
DEAD = {"dot": CRITICAL, "button": "widget.dead", "label": "widget.dead.hint"}

BAR_H = 8                  # meter thickness; r = h/2 gives the 4px rounded end
NO_DATA_TIP = "widget.no_data"
NO_FABLE_TIP = "widget.no_fable"
ICON_PATH = os.path.join(ROOT, "widget.ico")


def rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def ensure_icon():
    """Draw the icon once, pure stdlib: a mini widget card — dot + two meters."""
    if os.path.exists(ICON_PATH):
        return ICON_PATH
    import struct
    size, radius = 32, 7
    px = [[(0, 0, 0, 0)] * size for _ in range(size)]

    def in_card(x, y):
        if not (2 <= x <= 29 and 2 <= y <= 29):
            return False
        dx = max(2 + radius - x, 0, x - (29 - radius))
        dy = max(2 + radius - y, 0, y - (29 - radius))
        return dx * dx + dy * dy <= radius * radius

    card, track = rgb(RAISED), rgb(EDGE)
    for y in range(size):
        for x in range(size):
            if in_card(x, y):
                px[y][x] = (*card, 255)
    for y in range(size):
        for x in range(size):
            if (x - 9) ** 2 + (y - 9) ** 2 <= 9:        # presence dot
                px[y][x] = (*rgb(GOOD), 255)
    for y0, fill_to, color in ((16, 21, rgb(ACCENT)), (22, 15, rgb(GOOD))):
        for y in range(y0, y0 + 4):                     # meter: track + fill
            for x in range(6, 27):
                px[y][x] = (*color, 255) if x <= fill_to else (*track, 255)

    xor = b"".join(bytes((b, g, r, a))
                   for row in reversed(px) for r, g, b, a in row)
    mask = b"\x00" * (4 * size)
    bih = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                      len(xor) + len(mask), 0, 0, 0, 0)
    image = bih + xor + mask
    data = (struct.pack("<HHH", 0, 1, 1) +
            struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(image), 22) +
            image)
    with open(ICON_PATH, "wb") as f:
        f.write(data)
    return ICON_PATH


# 3x5 pixel digits, scaled up when drawn. A bitmap font is the only way to
# put a number on the tray icon without a font renderer or a dependency.
DIGITS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
}
TRAY_ICON_PATH = os.path.join(ROOT, "widget-tray.ico")


def _ico_bytes(px, size):
    """Pack a square RGBA pixel grid into a one-image .ico file."""
    import struct
    xor = b"".join(bytes((b, g, r, a))
                   for row in reversed(px) for r, g, b, a in row)
    mask = b"\x00" * (4 * size)
    bih = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                      len(xor) + len(mask), 0, 0, 0, 0)
    image = bih + xor + mask
    return (struct.pack("<HHH", 0, 1, 1) +
            struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(image), 22) +
            image)


def tray_icon(percent, color, path=TRAY_ICON_PATH):
    """The session percentage, drawn large enough to read in the tray.

    100 is shown as 99: three digits at this size are a smear, and the one
    percent of difference never changes what you do about it.
    """
    size, radius = 32, 7
    px = [[(0, 0, 0, 0)] * size for _ in range(size)]
    card = rgb(RAISED)
    for y in range(size):
        for x in range(size):
            dx = max(2 + radius - x, 0, x - (29 - radius))
            dy = max(2 + radius - y, 0, y - (29 - radius))
            if 2 <= x <= 29 and 2 <= y <= 29 and dx * dx + dy * dy <= radius ** 2:
                px[y][x] = (*card, 255)

    text = f"{max(0, min(99, int(round(percent)))):d}"
    scale = 4
    glyph_w, gap = 3 * scale, 2
    total = len(text) * glyph_w + (len(text) - 1) * gap
    left, top = (size - total) // 2, (size - 5 * scale) // 2
    ink = rgb(color)
    for index, char in enumerate(text):
        rows = DIGITS.get(char)
        if not rows:
            continue
        ox = left + index * (glyph_w + gap)
        for ry, row in enumerate(rows):
            for rx, bit in enumerate(row):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        x, y = ox + rx * scale + dx, top + ry * scale + dy
                        if 0 <= x < size and 0 <= y < size:
                            px[y][x] = (*ink, 255)
    with open(path, "wb") as f:
        f.write(_ico_bytes(px, size))
    return path


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]


def work_area(x, y):
    """The usable screen around a point: the monitor it is on, minus the
    taskbar. Tk only knows the primary display, which puts menus off-screen
    on a second monitor and under the taskbar on the first."""
    try:
        user32 = ctypes.windll.user32
        point = ctypes.wintypes.POINT(int(x), int(y))
        monitor = user32.MonitorFromPoint(point, 2)      # NEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcWork
            return r.left, r.top, r.right, r.bottom
    except (OSError, AttributeError, ValueError):
        pass
    return 0, 0, 1920, 1080


def round_window(win, radius=14):
    """Borderless windows get their rounding from a Win32 region."""
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    if w <= 1:
        return
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
    region = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius, radius)
    user32.SetWindowRgn(hwnd, region, True)


CARD_RADIUS = 22           # the widget card, and every window it opens
MENU_RADIUS = 10           # popup menus and their submenus


class EdgeRing:
    """The outline of a rounded window, drawn as a window of its own.

    Tk's own border is a rectangle drawn inside the window, so the region that
    rounds the corners slices it away — the outline ran along the straight
    edges and stopped where the curve began. This window is one pixel larger
    all round and cut to a ring of the same shape, so it follows the curve all
    the way. It is parked just underneath the window it outlines and tracks
    every move and resize of it.
    """

    def __init__(self, target, radius, alpha=1.0):
        self.target = target
        self.radius = radius
        self.win = tk.Toplevel(target)
        # Its own title: it inherits the target's otherwise, and then anything
        # looking that window up by name finds this ring instead.
        self.win.title("Claude ↔ Telegram edge")
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", alpha)
        self.win.configure(bg=EDGE)
        target.bind("<Configure>", self._on_configure, add="+")
        self.follow()

    def _on_configure(self, event):
        # <Configure> reaches the toplevel from every child that settles into
        # place too; only the window's own is about its geometry.
        if event.widget is self.target:
            self.follow()

    def follow(self):
        """Wrap the ring around the target and keep it underneath."""
        try:
            if not self.target.winfo_viewable():
                self.win.withdraw()
                return
            w, h = self.target.winfo_width(), self.target.winfo_height()
            if w <= 1:
                return
            x, y = self.target.winfo_rootx() - 1, self.target.winfo_rooty() - 1
            if not self.win.winfo_viewable():
                self.win.deiconify()
            self.win.geometry(f"{w + 2}x{h + 2}+{x}+{y}")
            self.win.update_idletasks()
            user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
            hwnd = user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            outer = gdi32.CreateRoundRectRgn(0, 0, w + 3, h + 3,
                                             self.radius + 1, self.radius + 1)
            # Hollow, so the target's own holes are not backed by this window.
            hole = gdi32.CreateRoundRectRgn(1, 1, w + 2, h + 2,
                                            self.radius, self.radius)
            gdi32.CombineRgn(outer, outer, hole, 4)     # RGN_DIFF
            gdi32.DeleteObject(hole)
            user32.SetWindowRgn(ctypes.c_void_p(hwnd), outer, True)
            self.win.lower(self.target)
        except tk.TclError:
            pass

    def restack(self):
        """Back under the target, after something raised it."""
        try:
            self.win.lower(self.target)
        except tk.TclError:
            pass

    def set_alpha(self, alpha):
        try:
            self.win.attributes("-alpha", alpha)
        except tk.TclError:
            pass

    def destroy(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass


def round_edge(win, radius, alpha=1.0):
    """Round a borderless window and outline it along the curve.

    The ring is left on the window so that raising one raises the other, and
    so that destroying the window takes its outline with it.
    """
    round_window(win, radius)
    win.ring = EdgeRing(win, radius, alpha)
    return win.ring


def lift_window(win):
    """Raise a window, taking its outline along."""
    win.lift()
    ring = getattr(win, "ring", None)
    if ring is not None:
        ring.restack()


GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020


def click_through(win, enabled):
    """Make the card invisible to the mouse, or solid again.

    WS_EX_TRANSPARENT takes the window out of hit-testing entirely: clicks,
    drags and hovers all land on whatever is underneath. WS_EX_LAYERED is
    never cleared — the alpha setting relies on it.

    The card cannot be clicked back on, so the tray icon is the way out:
    right-click it for the same menu.
    """
    user32 = ctypes.windll.user32
    hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
    get = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    put = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get.restype, put.restype = ctypes.c_ssize_t, ctypes.c_ssize_t
    style = get(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
    style = (style | WS_EX_LAYERED | WS_EX_TRANSPARENT) if enabled \
        else (style & ~WS_EX_TRANSPARENT)
    put(ctypes.c_void_p(hwnd), GWL_EXSTYLE, ctypes.c_ssize_t(style))


def start_card_drag(event, win):
    win._drag = (event.x_root - win.winfo_x(), event.y_root - win.winfo_y())


def card_drag(event, win):
    dx, dy = getattr(win, "_drag", (0, 0))
    win.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")


SHADOW = "#0d0d0c"         # drop shadow under the section labels


class ShadowText(tk.Canvas):
    """A text label with a drop shadow. A tk.Label cannot draw one, and the
    shadow is what keeps these labels legible once the card turns red."""

    def __init__(self, parent, font, pad=6):
        super().__init__(parent, bg=SURFACE, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.font = font
        self.pad = pad

    def render(self, text, fill, background, glow=None):
        """Draw the label. `glow` paints a soft halo instead of the shadow.

        Tk canvas text has no alpha, so the halo is two rings of the same
        text mixed toward the background — the far one fainter — which reads
        as a glow rather than an outline.
        """
        self.configure(bg=background)
        width = self.font.measure(text) + self.pad * 2
        height = self.font.metrics("linespace") + 3
        self.configure(width=width, height=height)
        self.delete("all")
        x, y = self.pad, height // 2

        def write(dx, dy, color):
            self.create_text(x + dx, y + dy, text=text, font=self.font,
                             fill=color, anchor="w")

        if glow:
            for spread, mix in ((2, 0.78), (1, 0.45)):
                ring = blend(glow, background, mix)
                for dx in (-spread, 0, spread):
                    for dy in (-spread, 0, spread):
                        if dx or dy:
                            write(dx, dy, ring)
        else:
            write(1, 1, SHADOW)
        write(0, 0, fill)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds():
    """System-wide time since the last key press or mouse move."""
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    ticks = ctypes.windll.kernel32.GetTickCount()
    return ((ticks - info.dwTime) & 0xFFFFFFFF) / 1000.0


class Tray(threading.Thread):
    """System tray icon on raw ctypes: stdlib-only, no pystray.

    Runs its own message loop in a daemon thread; that loop also hosts the
    low-level keyboard hook the auto-presence return needs (a key press is
    deliberate, a mouse nudge is not). The hook only notes THAT a key went
    down, never which one. Tk is not thread-safe, so events are handed to
    the widget as plain flags its own loop polls.
    """

    WM_TRAY = 0x8001          # WM_APP + 1

    def __init__(self, on_event, on_key, on_error=None):
        super().__init__(daemon=True)
        self.on_event = on_event
        self.on_key = on_key
        self.on_error = on_error
        self.hwnd = None
        self.hicon = None

    def run(self):
        # A thread that dies takes the tray icon with it and says nothing;
        # the icon simply never appears. Report it instead.
        try:
            self._run()
        except Exception:
            import traceback
            if self.on_error:
                self.on_error("tray thread died:\n" + traceback.format_exc())
            raise

    def _run(self):
        user32 = ctypes.windll.user32
        LRESULT = ctypes.c_ssize_t
        for fn in (user32.DefWindowProcW, user32.CreateWindowExW,
                   user32.LoadImageW, user32.SetWindowsHookExW,
                   user32.CallNextHookEx):
            fn.restype = ctypes.c_void_p
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p, ctypes.c_uint,
                                     ctypes.c_size_t, ctypes.c_ssize_t)
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, ctypes.c_size_t,
                                      ctypes.c_ssize_t)

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == self.WM_TRAY:
                if lparam == 0x0202:          # WM_LBUTTONUP
                    self.on_event("toggle")
                elif lparam == 0x0205:        # WM_RBUTTONUP
                    self.on_event("menu")
                return 0
            return user32.DefWindowProcW(ctypes.c_void_p(hwnd), msg,
                                         ctypes.c_size_t(wparam or 0),
                                         ctypes.c_ssize_t(lparam or 0)) or 0

        def kb_proc(code, wparam, lparam):
            if code >= 0 and wparam in (0x0100, 0x0104):   # KEYDOWN, SYSKEYDOWN
                self.on_key()
            return user32.CallNextHookEx(None, code,
                                         ctypes.c_size_t(wparam or 0),
                                         ctypes.c_ssize_t(lparam or 0)) or 0

        # Button presses only, never movement: a nudged desk would otherwise
        # pull the session out of Telegram while you are still away, and a
        # callback on every mouse move is a needless tax on the whole system.
        BUTTONS = (0x0201, 0x0204, 0x0207, 0x020B)   # L, R, M, X button down

        def mouse_proc(code, wparam, lparam):
            if code >= 0 and wparam in BUTTONS:
                self.on_key()
            return user32.CallNextHookEx(None, code,
                                         ctypes.c_size_t(wparam or 0),
                                         ctypes.c_ssize_t(lparam or 0)) or 0

        self.proc = WNDPROC(wnd_proc)         # keep alive or they are GC'd
        self.kb = HOOKPROC(kb_proc)
        self.mouse = HOOKPROC(mouse_proc)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                        ("hCursor", ctypes.c_void_p),
                        ("hbrBackground", ctypes.c_void_p),
                        ("lpszMenuName", ctypes.c_wchar_p),
                        ("lpszClassName", ctypes.c_wchar_p)]

        wc = WNDCLASSW()
        wc.lpfnWndProc = self.proc
        wc.lpszClassName = "ClaudeTgTrayWnd"
        # restype first: ctypes defaults to c_int, which truncates a 64-bit
        # module handle. Running from source the interpreter happened to load
        # low enough to survive it; a frozen build does not, and passing the
        # mangled handle on threw before the icon was ever created — the tray
        # thread died silently and the icon simply never appeared.
        kernel32 = ctypes.windll.kernel32
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        wc.hInstance = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, None, 0, 0, 0,
                                           0, 0, None, None,
                                           ctypes.c_void_p(wc.hInstance), None)
        try:
            self.hicon = user32.LoadImageW(None, ensure_icon(), 1, 0, 0, 0x10)
        except OSError as e:
            self.hicon, _ = None, self.on_error and self.on_error("icon: %s" % e)
        if not self.hicon:
            # Drawing our own failed — an unwritable folder, a corrupt file.
            # A stock icon still gives the user somewhere to right-click.
            user32.LoadIconW.restype = ctypes.c_void_p
            self.hicon = user32.LoadIconW(None, 32512)      # IDI_APPLICATION
            if self.on_error:
                self.on_error("tray: using the stock icon, %s was not usable"
                              % ICON_PATH)
        if not ctypes.windll.shell32.Shell_NotifyIconW(0, ctypes.byref(self._nid())):
            if self.on_error:
                self.on_error("tray: Shell_NotifyIcon refused to add the icon")
        # After the icon exists: the shell writes its settings entry when it
        # first sees it, so there is nothing to promote before this point.
        self.promote()
        self.hook = user32.SetWindowsHookExW(13, self.kb, None, 0)   # WH_KEYBOARD_LL
        self.mouse_hook = user32.SetWindowsHookExW(14, self.mouse, None, 0)  # WH_MOUSE_LL

        msg = ctypes.create_string_buffer(48)
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def promote(self):
        """Ask Windows 11 to show the icon instead of hiding it in the
        overflow flyout.

        New executables land there by default, which reads as "the app has no
        tray icon" — the icon exists, it is just behind a chevron nobody
        opens. Windows records the choice per executable under
        NotifyIconSettings; this only fills it in when it has never been set,
        so a deliberate "hide this" is left alone.
        """
        try:
            import winreg
        except ImportError:
            return
        exe = os.path.normcase(sys.executable)
        try:
            root = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                  r"Control Panel\NotifyIconSettings", 0,
                                  winreg.KEY_READ)
        except OSError:
            return                      # older Windows: nothing to promote
        promoted = False
        with root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                try:
                    name = winreg.EnumKey(root, index)
                    with winreg.OpenKey(root, name, 0,
                                        winreg.KEY_READ | winreg.KEY_SET_VALUE) as item:
                        path, _ = winreg.QueryValueEx(item, "ExecutablePath")
                        if os.path.normcase(str(path)) != exe:
                            continue
                        try:
                            winreg.QueryValueEx(item, "IsPromoted")
                            continue    # already decided, by us or by the user
                        except OSError:
                            pass
                        winreg.SetValueEx(item, "IsPromoted", 0,
                                          winreg.REG_DWORD, 1)
                        promoted = True
                except OSError:
                    continue
        if promoted:
            # Re-register so the shell re-reads the setting instead of waiting
            # for the next sign-in.
            shell32 = ctypes.windll.shell32
            shell32.Shell_NotifyIconW(2, ctypes.byref(self._nid()))   # DELETE
            shell32.Shell_NotifyIconW(0, ctypes.byref(self._nid()))   # ADD
            if self.on_error:
                self.on_error("tray: icon promoted out of the overflow flyout")

    def _nid(self):
        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("hWnd", ctypes.c_void_p),
                        ("uID", ctypes.c_uint), ("uFlags", ctypes.c_uint),
                        ("uCallbackMessage", ctypes.c_uint),
                        ("hIcon", ctypes.c_void_p),
                        ("szTip", ctypes.c_wchar * 128)]
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = 0x7                      # MESSAGE | ICON | TIP
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = "Claude ↔ Telegram"
        return nid

    def set_percent(self, percent, color):
        """Redraw the tray icon with the current session percentage.

        Called from the Tk thread rather than this one: Shell_NotifyIcon talks
        to the shell, not to our own message loop, so it does not need to run
        where the window lives.
        """
        if not self.hwnd:
            return
        try:
            path = tray_icon(percent, color)
            user32 = ctypes.windll.user32
            user32.LoadImageW.restype = ctypes.c_void_p
            icon = user32.LoadImageW(None, path, 1, 0, 0, 0x10)
            if not icon:
                return
            previous, self.hicon = self.hicon, icon
            ctypes.windll.shell32.Shell_NotifyIconW(1, ctypes.byref(self._nid()))
            if previous:
                # Only after the shell has the new one, or the tray blinks.
                user32.DestroyIcon(ctypes.c_void_p(previous))
        except OSError:
            pass

    def remove(self):
        if self.hwnd:
            try:
                ctypes.windll.shell32.Shell_NotifyIconW(2, ctypes.byref(self._nid()))
            except OSError:
                pass


class Tooltip:
    """Hover hint for widgets that have no room for a caption."""

    def __init__(self, widget, text_getter, font):
        self.widget = widget
        self.get = text_getter
        self.font = font
        self.tip = None
        self.label = None
        self.job = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")
        widget.bind("<Button-1>", self.hide, add="+")

    def schedule(self, _event=None):
        self.cancel()
        self.job = self.widget.after(350, self.show)

    def cancel(self):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None

    def show(self):
        text = self.get()
        if not text or self.tip:
            return
        x, y = self.widget.winfo_pointerxy()
        self.tip = tk.Toplevel(self.widget)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.label = tk.Label(self.tip, text=text, font=self.font, bg=RAISED,
                              fg=SECONDARY, padx=8, pady=4, justify="left",
                              highlightthickness=1, highlightbackground=EDGE)
        self.label.pack()
        self.tip.geometry(f"+{x + 12}+{y + 16}")

    def refresh(self):
        """Live state changed under the cursor: update the open tip, not lie."""
        if self.tip:
            text = self.get()
            if text:
                self.label.configure(text=text)
            else:
                self.hide()

    def hide(self, _event=None):
        self.cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


class PopupMenu:
    """Hand-drawn dark popup: tk.Menu on Windows always paints a white system
    border, so the menu is built from plain windows instead. It hides shortly
    after the pointer leaves it, on a click, or when a new one opens.

    Items: ("cmd", label, callback) · ("sep",) · ("sub", label, items)
           ("radio", label, tk_variable, value, callback)
    """

    def __init__(self, root, font):
        self.root = root
        self.font = font
        self.wins = []
        self.hide_job = None
        self.sub_job = None        # pending "the pointer left the submenu"
        self.sub_owner = None      # the row whose submenu is currently open

    def popup(self, x, y, items):
        self.close()
        self.wins = [self._build(items, x, y)]
        # No grab. A grab would close the menu on an outside click, but Tk
        # only delivers events inside the grab tree, and a submenu is the
        # parent's sibling — grabbing for it froze the menu it came from.
        # <Button> is not bound app-wide anywhere else, so unbind_all on close
        # takes back exactly this one.
        self.wins[0].bind_all("<Button>", self._maybe_dismiss, add="+")

    def _maybe_dismiss(self, event):
        if any(self._inside(win, event.x_root, event.y_root)
               for win in self.wins):
            return          # a real menu row: its own handler deals with it
        self.close()

    @staticmethod
    def _inside(win, x, y):
        try:
            left, top = win.winfo_rootx(), win.winfo_rooty()
            return (left <= x < left + win.winfo_width()
                    and top <= y < top + win.winfo_height())
        except tk.TclError:
            return False

    def _build(self, items, x, y):
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        # No highlight border: it is a rectangle, and the rounded region cuts
        # its corners off. The outline is drawn by a ring window instead.
        win.configure(bg=SURFACE, highlightthickness=0)
        win.bind("<Enter>", lambda e, w=win: self._on_enter(w), add="+")
        win.bind("<Leave>", lambda e: self._schedule_hide(), add="+")
        for item in items:
            self._row(win, item)
        win.geometry(f"+{x}+{y}")
        self._fit_on_screen(win, x, y)
        # Once the rows have settled: the ring is cut to the menu's real size.
        win.after(10, lambda: round_edge(win, MENU_RADIUS))
        return win

    @staticmethod
    def _fit_on_screen(win, x, y):
        """Keep the menu inside the monitor it opened on.

        The tray sits at the bottom of the screen, so a menu drawn downwards
        from the cursor ran straight off it and the last items were simply
        unreachable. Pulling it back inside the work area covers that, the
        right edge, and a second monitor, without special cases.
        """
        win.update_idletasks()
        width, height = win.winfo_reqwidth(), win.winfo_reqheight()
        left, top, right, bottom = work_area(x, y)
        if y + height > bottom:
            y = bottom - height
        if x + width > right:
            x = right - width
        win.geometry(f"+{int(max(x, left))}+{int(max(y, top))}")

    def _row(self, win, item):
        kind = item[0]
        if kind == "sep":
            tk.Frame(win, height=1, bg=HAIRLINE).pack(fill="x", padx=8, pady=3)
            return
        if kind == "radio":
            _, label, var, value, command = item
            marked = "●" if var.get() == value else " "
            text = f" {marked}  {label}"
            action = lambda: (var.set(value), command())
            arrow = None
        elif kind == "sub":
            _, label, sub_items = item
            text = f"    {label}"
            action = None
            arrow = sub_items
        else:
            _, label, command = item
            text = f"    {label}"
            action = command
            arrow = None

        row = tk.Frame(win, bg=SURFACE)
        row.pack(fill="x")
        lab = tk.Label(row, text=text, font=self.font, bg=SURFACE, fg=SECONDARY,
                       anchor="w", padx=10, pady=3)
        lab.pack(side="left", fill="x", expand=True)
        tail = None
        if arrow is not None:
            tail = tk.Label(row, text="▸", font=self.font, bg=SURFACE,
                            fg=MUTED, padx=8)
            tail.pack(side="right")

        def paint(hover):
            bg = RAISED_HI if hover else SURFACE
            row.configure(bg=bg)
            lab.configure(bg=bg, fg=PRIMARY if hover else SECONDARY)
            if tail is not None:
                tail.configure(bg=bg)

        def enter(_e=None):
            paint(True)
            self._cancel_hide()
            if arrow is None:
                # A plain row does not close the submenu outright: the
                # pointer's diagonal path toward it grazes the neighbours, and
                # closing on every graze made it flicker away mid-flight. The
                # delayed close rechecks where the pointer actually ended up.
                self._schedule_sub_close()
                return
            self._cancel_sub_close()
            if (self.sub_owner is row
                    and len(self.wins) > self.wins.index(win) + 1):
                return              # this row's submenu is already up
            self._close_from(win)
            self.sub_owner = row
            sub_x = win.winfo_rootx() + win.winfo_width() - 8
            sub_y = row.winfo_rooty() - 4
            self.wins.append(self._build(arrow, sub_x, sub_y))

        def click(_e=None):
            if action is not None:
                self.close()
                action()

        for w in (row, lab) + ((tail,) if tail is not None else ()):
            w.bind("<Enter>", enter)
            w.bind("<Leave>", lambda _e=None: paint(False))
            w.bind("<Button-1>", click)

    def _close_from(self, win):
        """Closing a submenu chain when the pointer moves to another row."""
        index = self.wins.index(win) if win in self.wins else -1
        for extra in self.wins[index + 1:]:
            extra.destroy()
        self.wins = self.wins[:index + 1]

    def _on_enter(self, win):
        """A window (or any row inside it — Enter reaches the toplevel through
        its bindtag) got the pointer.

        Only the submenu's own window calls off a pending close. Letting the
        parent's window do it too undid the close that its own plain rows had
        just scheduled, and the submenu never folded away.
        """
        self._cancel_hide()
        if self.wins and win is not self.wins[0]:
            self._cancel_sub_close()

    def _schedule_sub_close(self):
        """Fold an open submenu away once the pointer settles elsewhere."""
        self._cancel_sub_close()
        self.sub_job = self.root.after(320, self._close_sub_if_away)

    def _cancel_sub_close(self):
        if self.sub_job:
            self.root.after_cancel(self.sub_job)
            self.sub_job = None

    def _close_sub_if_away(self):
        """Close the submenu unless the pointer reached it, or came back to
        the row that owns it — the same 'ask the pointer' rule as hiding."""
        self.sub_job = None
        if len(self.wins) < 2:
            return
        x, y = self.root.winfo_pointerxy()
        if any(self._inside(win, x, y) for win in self.wins[1:]):
            return
        if self.sub_owner is not None and self._inside(self.sub_owner, x, y):
            return
        self._close_from(self.wins[0])
        self.sub_owner = None

    def _schedule_hide(self):
        self._cancel_hide()
        self.hide_job = self.root.after(600, self._hide_if_away)

    def _hide_if_away(self):
        """Close only if the pointer really left.

        Moving the grab to a freshly opened submenu makes Tk deliver a
        <Leave> to the parent even though the pointer never moved off it —
        trusting that event closed the whole chain on the way to a submenu.
        The pointer's actual position cannot lie.
        """
        self.hide_job = None
        if not self.wins:
            return
        x, y = self.root.winfo_pointerxy()
        if any(self._inside(win, x, y) for win in self.wins):
            self._schedule_hide()       # still on the menu: keep it up
            return
        self.close()

    def _cancel_hide(self):
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
            self.hide_job = None

    def close(self):
        self._cancel_hide()
        self._cancel_sub_close()
        if self.wins:
            try:
                self.wins[0].unbind_all("<Button>")
            except tk.TclError:
                pass
        for win in self.wins:
            try:
                win.destroy()
            except tk.TclError:
                pass
        self.wins = []
        self.sub_owner = None


class Widget:
    def __init__(self):
        cfg = load_cfg()
        i18n.set_language(cfg.get("language"))
        host = cfg.get("host", "127.0.0.1")
        port = cfg.get("port", 8787)
        self.base = f"http://{host}:{port}"
        # No max_age here on purpose: a reading is worth showing until its own
        # window turns over, which usage.expire_spent_windows works out per
        # window. Hiding everything an hour after capture is what left the card
        # blank for a day when the poll could not get through.
        self.alive = False
        self.busy = False
        self.away_state = False
        self.theme = AT_PC
        self.hover = False
        self.tips = {}
        self.drag = None
        self.last_key_ts = 0.0
        self.surface = SURFACE      # the card's current background, alarm aside

        saved = self.load_saved()
        self.root = tk.Tk()
        self.root.title("Claude ↔ Telegram")
        self.root.report_callback_exception = self.report_callback_exception
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        # No highlight border: it is a rectangle, and the rounded region cuts
        # its corners off. The outline is drawn by a ring window instead.
        self.root.configure(bg=SURFACE, highlightthickness=0)
        self.root.geometry(f"+{int(saved.get('x', 40))}+{int(saved.get('y', 40))}")
        self.alpha_var = tk.DoubleVar(value=float(saved.get("alpha", 0.8)))
        self.root.attributes("-alpha", self.alpha_var.get())
        self.auto_var = tk.IntVar(value=int(saved.get("auto_away_seconds", 300)))
        self.auto_away_active = False
        self.through_var = tk.IntVar(value=int(saved.get("click_through", 0)))
        self.punches = []          # solid stand-ins for the live controls
        self.edge = None           # the card's outline, drawn as a ring
        self.overlays = []         # cards that must stay above them
        self.tray_percent = None                # last value drawn on the icon
        self.fable_pct = None                   # last Fable reading, for the tab
        self.fable_alarm = False                # Fable spent: unfold regardless
        try:
            self.root.iconbitmap(default=ensure_icon())
        except tk.TclError:
            pass

        bold = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        small = tkfont.Font(family="Segoe UI", size=8)
        value = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        gear_font = tkfont.Font(family="Segoe UI", size=12)
        self.small = small
        self.bold = bold

        # -- top row: presence capsule stretched up to the gear -----------
        top = tk.Frame(self.root, bg=SURFACE)
        top.pack(fill="x", padx=10, pady=(9, 2))
        self.gear_font = gear_font
        self.gear = tk.Label(top, text="⚙", font=gear_font, bg=SURFACE,
                             fg=MUTED, cursor="hand2")
        self.gear.pack(side="right", padx=(0, 2))
        self.gear.bind("<Button-1>", self.open_menu)
        self.gear.bind("<Enter>", lambda e: self.gear.configure(fg=PRIMARY))
        self.gear.bind("<Leave>", lambda e: self.gear.configure(fg=MUTED))
        # Right gap of the gear is 2+10=12px; mirror it on its left.
        # Tiny requested width: fill/expand stretches it to the real room,
        # while the meters below dictate the window's width.
        self.toggle = tk.Canvas(top, width=60, height=30, bg=SURFACE,
                                highlightthickness=0, cursor="hand2")
        self.toggle.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.toggle.bind("<Button-1>", lambda e: self.on_toggle())
        self.toggle.bind("<Enter>", lambda e: self.set_hover(True))
        self.toggle.bind("<Leave>", lambda e: self.set_hover(False))
        self.toggle.bind("<Configure>", lambda e: self.paint_toggle())

        self.lang_var = tk.StringVar(value=cfg.get("language") or "auto")
        self.toggle_tip = Tooltip(self.toggle, lambda: t(self.theme["label"]), small)

        tk.Frame(self.root, height=1, bg=HAIRLINE).pack(fill="x", padx=10,
                                                        pady=(7, 0))

        # -- limits header: the Fable row folds away here -----------------
        self.fable_shown = tk.IntVar(value=int(saved.get("fable_shown", 0)))
        self.limits_shown = tk.IntVar(value=int(saved.get("limits_shown", 1)))
        self.last_usage = None
        # -- alarm, on a line of its own ----------------------------------
        # It used to be the last word of the session list, which is cut at 48
        # characters — so the alarm was the part that got cut. Packed only
        # while one is set, above the limits, where nothing else moves.
        self.alarm_label = tk.Label(self.root, text="", font=small, bg=SURFACE,
                                    fg=SECONDARY, anchor="w", padx=14)

        limits_bar = tk.Frame(self.root, bg=SURFACE)
        limits_bar.pack(fill="x", padx=14, pady=(6, 0))
        self.limits_tab = ShadowText(limits_bar, small)
        self.limits_tab.pack(side="left")
        self.limits_tab.bind("<Button-1>",
                             lambda e: (self.toggle_limits(), "break")[1])
        self.fable_tab = ShadowText(limits_bar, small)
        self.fable_tab.pack(side="right")
        self.fable_tab.bind("<Button-1>",
                            lambda e: (self.toggle_fable(), "break")[1])
        self.limits_bar = limits_bar

        # -- usage meters -------------------------------------------------
        # Fable shares the 5-hour window with everything else and only has a
        # weekly limit of its own, so it is one extra row rather than a
        # separate pool: switching views used to hide the numbers that were
        # still ticking.
        meters = tk.Frame(self.root, bg=SURFACE)
        meters.pack(fill="x", padx=14, pady=(5, 5))
        meters.columnconfigure(1, weight=1)
        self.meters_frame = meters
        self.meters = {}
        # Every meter shares the calm blue; only the ramp above it differs.
        rows = (("five_hour", t("widget.meter.five_hour"), session_color),
                ("seven_day", t("widget.meter.seven_day"), severity),
                ("fable", t("widget.meter.fable"), severity))
        for row, (key, label, ramp) in enumerate(rows):
            caption = tk.Label(meters, text=label, font=small, bg=SURFACE,
                               fg=SECONDARY, anchor="w", width=8)
            caption.grid(row=row, column=0, sticky="w", pady=2)
            bar = tk.Canvas(meters, width=170, height=BAR_H, bg=SURFACE,
                            highlightthickness=0)
            bar.grid(row=row, column=1, sticky="ew", padx=(2, 8), pady=2)
            pct = tk.Label(meters, text="—", font=value, bg=SURFACE, fg=PRIMARY,
                           anchor="e", width=5)
            pct.grid(row=row, column=2, sticky="e", pady=2)
            self.meters[key] = {"bar": bar, "pct": pct, "caption": caption,
                                "ramp": ramp}
            self.tips[key] = t(NO_DATA_TIP)
            Tooltip(bar, lambda k=key: self.tips[k], small)
            Tooltip(pct, lambda k=key: self.tips[k], small)
        self.layout_limits()

        tk.Frame(self.root, height=1, bg=HAIRLINE).pack(fill="x", padx=10)
        self.info_full = ""
        self.info_label = tk.Label(self.root, text="", font=small, bg=SURFACE,
                                   fg=MUTED, anchor="w", padx=14)
        self.info_label.pack(fill="x", pady=(4, 6))
        self.info_tip = Tooltip(self.info_label, lambda: self.info_full, small)

        # -- borderless plumbing: menu everywhere, drag on passive parts --
        self.pop = PopupMenu(self.root, small)
        for area in (self.root, top, self.info_label, meters, limits_bar,
                     *[m[part] for m in self.meters.values()
                       for part in ("bar", "pct", "caption")]):
            area.bind("<Button-3>", self.open_menu)
            area.bind("<Button-1>", self.start_drag, add="+")
            area.bind("<B1-Motion>", self.on_drag, add="+")
            area.bind("<ButtonRelease-1>", self.end_drag, add="+")

        self._region = (0, 0)
        self.root.bind("<Configure>", self.round_corners)
        self.tray_event = None
        self.tray = Tray(self.on_tray, self.on_key, self.trace)
        self.tray.start()
        self.root.after(150, self.tray_pump)
        # After the window really exists, or there is no hwnd to restyle.
        self.root.after(120, self.build_edge)
        # A fresh install has nothing to show and no obvious next step, so the
        # setup screen opens itself rather than waiting to be found in a menu.
        self.root.after(900, self.offer_setup)
        self.root.after(200, self.apply_click_through)
        self.paint_toggle()
        self.poll()

    # -- menu -------------------------------------------------------------

    def menu_items(self):
        alpha = [("radio", f"{100 - int(opacity * 100)}%", self.alpha_var,
                  opacity, self.set_alpha)
                 for opacity in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5)]
        auto = [("radio", t("menu.off"), self.auto_var, 0, self.save_pos)]
        auto += [("radio", t("menu.min", n=seconds // 60), self.auto_var,
                  seconds, self.save_pos)
                 for seconds in (60, 120, 300, 600, 900)]
        langs = [("radio", t("menu.lang.auto"), self.lang_var, "auto",
                  lambda: self.set_lang("auto"))]
        langs += [("radio", i18n.NATIVE[code], self.lang_var, code,
                   lambda code=code: self.set_lang(code))
                  for code in i18n.LANGUAGES]
        through = [("radio", t("menu.off"), self.through_var, 0,
                    self.apply_click_through),
                   ("radio", t("menu.on"), self.through_var, 1,
                    self.apply_click_through)]
        return [
            ("cmd", t("menu.setup"), self.open_setup),
            ("cmd", t("menu.projects"), self.open_projects),
            ("cmd", t("menu.settings"), self.open_settings),
            ("cmd", t("menu.alarm"), self.open_alarm),
            ("cmd", t("menu.usage"), self.refresh_usage),
            ("sub", t("menu.alpha"), alpha),
            ("sub", t("menu.clickthrough"), through),
            ("sub", t("menu.autoaway"), auto),
            ("sub", t("menu.language"), langs),
            ("sep",),
            ("cmd", t("menu.tray"), self.hide_to_tray),
            ("cmd", t("menu.close"), self.close),
        ]

    def refresh_usage(self):
        """Ask for the limits now. The poll runs on its own schedule and the
        server can hold it off for half an hour, so this is the way to say
        "look again" without restarting anything."""
        self.paint(self.theme, t("usage.asking"))

        def work():
            answer = self.request("/usage", {})
            self.root.after(0, lambda: self.show_refresh(answer))
        threading.Thread(target=work, daemon=True).start()

    def show_refresh(self, answer):
        if answer is None:
            return self.paint(DEAD, t("widget.dead.settings"))
        data = usage.expire_spent_windows(answer.get("usage"))
        self.apply_usage(data)          # the meters follow immediately
        if answer.get("ok"):
            self.paint(self.theme, usage_summary(data))
        elif answer.get("wait"):
            # The server sets the pace; asking again only lengthens it.
            self.paint(self.theme, t("setup.usage.wait",
                                     minutes=max(1, int(answer["wait"]) // 60)))
        else:
            self.paint(self.theme, answer.get("error") or t("setup.usage.failed"))

    def set_lang(self, code):
        """Persist via the daemon (it owns config.json), then restart the
        widget: every caption is drawn once, a restart is the honest redraw."""
        lang = code if code in i18n.LANGUAGES else None
        if self.request("/settings", {"settings": {"language": lang}}) is None:
            try:        # daemon down: write the config directly
                with open(CONFIG, encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg["language"] = lang
                tmp = CONFIG + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                os.replace(tmp, CONFIG)
            except (OSError, ValueError):
                pass
        subprocess.Popen([sys.executable, os.path.join(ROOT, "widget.pyw")],
                         cwd=ROOT)
        self.close()

    def open_menu(self, event=None):
        if event is not None:
            x, y = event.x_root, event.y_root
        else:
            x, y = self.gear.winfo_rootx(), self.gear.winfo_rooty() + 20
        self.pop.popup(x, y, self.menu_items())
        return "break"      # a menu click must not start a window drag

    def on_tray(self, kind):
        # Called from the tray thread: only set a flag, Tk is not thread-safe.
        self.tray_event = kind

    def on_key(self):
        # Called from the keyboard hook (tray thread): a timestamp is enough.
        self.last_key_ts = time.time()

    def tray_pump(self):
        event, self.tray_event = self.tray_event, None
        if event == "toggle":
            self.toggle_visible()
        elif event == "menu":
            pt = (ctypes.c_long * 2)()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            self.pop.popup(pt[0], pt[1], self.menu_items())
        self.root.after(150, self.tray_pump)

    def hide_to_tray(self):
        self.root.withdraw()
        self.place_edge()
        self.place_punch()      # or the capsule stand-in stays on screen alone

    def toggle_visible(self):
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            # Some WMs drop these on deiconify; reassert both.
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
        else:
            self.root.withdraw()
        # The stand-in is a window of its own: hiding the card does not hide it.
        self.root.after(60, lambda: (self.place_edge(), self.place_punch()))

    # -- presence capsule ------------------------------------------------

    def set_hover(self, on):
        self.hover = on
        self.paint_toggle()

    def paint_toggle(self):
        self.paint_capsule(self.toggle)
        for punch in self.punches:
            if isinstance(punch["body"], tk.Canvas):
                self.paint_capsule(punch["body"])

    def capsule_fill(self):
        """The capsule is the raised step of whatever the card is wearing.

        Left neutral grey on an alarm background it reads as a hole punched in
        the card; derived from the alarm colour it stays a button.
        """
        if self.surface == SURFACE:
            return RAISED_HI if self.hover else RAISED
        return blend(self.surface, "#ffffff", 0.20 if self.hover else 0.11)

    def paint_capsule(self, c):
        c.delete("all")
        w, h = max(c.winfo_width(), 60), 30
        r = h // 2
        fill = self.capsule_fill()
        c.create_oval(0, 0, 2 * r, h, fill=fill, outline="")
        c.create_oval(w - 2 * r, 0, w, h, fill=fill, outline="")
        c.create_rectangle(r, 0, w - r, h, fill=fill, outline="")
        c.create_oval(14, 11, 22, 19, fill=self.theme["dot"], outline="")
        c.create_text(32, 15, text=t(self.theme["button"]), font=self.bold,
                      fill=PRIMARY, anchor="w")

    # -- borderless window plumbing --------------------------------------

    def round_corners(self, _event=None):
        w, h = self.root.winfo_width(), self.root.winfo_height()
        if w <= 1 or (w, h) == self._region:
            return
        self._region = (w, h)
        self.shape_card()
        self.place_edge()

    def build_edge(self):
        """The card's outline. The card shapes itself: `shape_card` punches
        holes in its region, which a ring of its own must not fill in."""
        self.edge = EdgeRing(self.root, CARD_RADIUS, self.alpha_var.get())

    def place_edge(self):
        if self.edge:
            self.edge.follow()

    def shape_card(self):
        """Rounded card region, with a hole under every stand-in.

        Both windows are layered at the same alpha, so stacking them made the
        covered area composite twice — visibly darker, worst at the corners
        where the stand-in's square background met the card. Cutting the card
        away underneath leaves exactly one layer everywhere.
        """
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        if w <= 1:
            return
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        region = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1,
                                          CARD_RADIUS, CARD_RADIUS)
        if self.punches:
            rect = (ctypes.c_int * 4)()
            user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
            for punch in self.punches:
                source = punch["source"]
                x = source.winfo_rootx() - rect[0]
                y = source.winfo_rooty() - rect[1]
                hole = gdi32.CreateRectRgn(x, y, x + source.winfo_width(),
                                           y + source.winfo_height())
                gdi32.CombineRgn(region, region, hole, 4)   # RGN_DIFF
                gdi32.DeleteObject(hole)
        # The system takes ownership of `region`; it must not be deleted here.
        user32.SetWindowRgn(ctypes.c_void_p(hwnd), region, True)

    def start_drag(self, event):
        self.drag = (event.x_root - self.root.winfo_x(),
                     event.y_root - self.root.winfo_y())

    def on_drag(self, event):
        if self.drag:
            dx, dy = self.drag
            self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")
            self.place_edge()

    def end_drag(self, _event):
        self.drag = None
        self.save_pos()

    # -- daemon I/O (off the UI thread) ---------------------------------

    def request(self, path, payload=None):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=4) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def push(self, path, payload):
        """Fire-and-forget write to the daemon, so a row click never blocks."""
        threading.Thread(target=self.request, args=(path, payload),
                         daemon=True).start()

    def poll(self):
        threading.Thread(target=self._poll_worker, daemon=True).start()
        # Cheap, and the only way the stand-in survives a moved or resized
        # card: in click-through mode the card cannot be dragged anyway, but
        # the Fable row folding in and out does move the capsule.
        self.place_edge()
        self.place_punch()
        self.root.after(POLL_MS, self.poll)

    def _poll_worker(self):
        snapshot = self.request("/mode")
        limits = usage.expire_spent_windows(usage.load())
        self.root.after(0, self.apply, snapshot)
        self.root.after(0, self.apply_usage, limits)

    def on_toggle(self):
        # A dead daemon means the click is "bring it up, at the PC".
        self.auto_away_active = False       # a manual click owns the state
        self.set_away(not self.away_state if self.alive else False)
        return "break"

    def set_away(self, away):
        self.busy = True
        threading.Thread(target=self._toggle_worker, args=(away,), daemon=True).start()

    def auto_presence(self, away):
        """Idle keyboard+mouse = gone: flip to away. A key press or a mouse
        CLICK flips back; movement alone does not, so a nudged desk cannot
        yank a session out of Telegram while you are still away. Manual
        switches are never undone automatically.
        """
        seconds = self.auto_var.get()
        if not seconds or not self.alive or self.busy:
            return
        if not away:
            if idle_seconds() >= seconds:
                self.auto_away_active = True
                self.set_away(True)
            else:
                self.auto_away_active = False
        elif self.auto_away_active and time.time() - self.last_key_ts < 5:
            self.auto_away_active = False
            self.set_away(False)

    def _toggle_worker(self, away):
        snapshot = self.request("/mode", {"away": away})
        if snapshot is None and self.start_daemon():
            snapshot = self.request("/mode", {"away": away})
        self.busy = False
        self.root.after(0, self.apply, snapshot)

    def start_daemon(self):
        command = paths.daemon_command()
        if not command:
            return False
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0)
        try:
            subprocess.Popen(command, cwd=ROOT,
                             creationflags=creation, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except OSError:
            return False
        for _ in range(16):
            time.sleep(0.5)
            if self.request("/mode") is not None:
                return True
        return False

    # -- cards: frameless windows in the widget's own style ---------------

    def make_card(self, title, width=440):
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        alpha = max(self.alpha_var.get(), 0.9)
        win.attributes("-alpha", alpha)
        # No highlight border: it is a rectangle, and the rounded region cuts
        # its corners off. The outline is drawn by a ring window instead.
        win.configure(bg=SURFACE, highlightthickness=0)
        win.geometry(f"{width}x420+{self.root.winfo_x() + 30}"
                     f"+{self.root.winfo_y() + 30}")

        head = tk.Frame(win, bg=SURFACE)
        head.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(head, text=title, font=self.bold, bg=SURFACE, fg=PRIMARY,
                 anchor="w").pack(side="left")
        closer = tk.Label(head, text="✕", font=self.small, bg=SURFACE,
                          fg=MUTED, cursor="hand2", padx=4)
        closer.pack(side="right")
        closer.bind("<Button-1>", lambda e: win.destroy())
        closer.bind("<Enter>", lambda e: closer.configure(fg=PRIMARY))
        closer.bind("<Leave>", lambda e: closer.configure(fg=MUTED))
        for w in (head,):
            w.bind("<Button-1>", lambda e: start_card_drag(e, win), add="+")
            w.bind("<B1-Motion>", lambda e: card_drag(e, win), add="+")
        win.bind("<Escape>", lambda e: win.destroy())
        # Rounded exactly like the widget card, outline included.
        win.after(10, lambda: round_edge(win, CARD_RADIUS, alpha))
        win.focus_force()
        # Registered so the stand-ins never climb over it when they reposition.
        self.overlays.append(win)
        win.bind("<Destroy>", lambda e, w=win: (w in self.overlays
                                                and self.overlays.remove(w)),
                 add="+")
        win.lift()
        return win

    def check_row(self, parent, text, checked, command, dim=False):
        """A themed checkbox row: Windows ignores Tk checkbutton colors."""
        row = tk.Frame(parent, bg=SURFACE, cursor="hand2")
        box = tk.Canvas(row, width=14, height=14, bg=SURFACE,
                        highlightthickness=0)
        box.pack(side="left", padx=(0, 7))
        label = tk.Label(row, text=text, font=self.small, bg=SURFACE,
                         fg=MUTED if dim else SECONDARY, anchor="w")
        label.pack(side="left", fill="x", expand=True)
        state = {"on": checked}

        def draw():
            box.delete("all")
            if state["on"]:
                box.create_rectangle(1, 1, 13, 13, fill=ACCENT, outline=ACCENT)
                box.create_line(4, 7, 6, 10, fill=PRIMARY, width=2)
                box.create_line(6, 10, 11, 4, fill=PRIMARY, width=2)
            else:
                box.create_rectangle(1, 1, 13, 13, fill=RAISED, outline=EDGE)

        def click(_event=None):
            state["on"] = not state["on"]
            draw()
            command(state["on"])
            return "break"

        for w in (row, box, label):
            w.bind("<Button-1>", click)
            w.bind("<Enter>", lambda e: label.configure(fg=PRIMARY))
            w.bind("<Leave>", lambda e: label.configure(
                fg=MUTED if dim else SECONDARY))
        draw()
        return row

    def open_projects(self):
        snapshot = self.request("/projects")
        if snapshot is None:
            return self.paint(DEAD, t("widget.dead.projects"))
        projects = snapshot.get("projects") or []
        win = self.make_card(t("card.projects"))
        tk.Label(win, text=t("card.projects.hint"),
                 font=self.small, bg=SURFACE, fg=MUTED, anchor="w", padx=14,
                 wraplength=400, justify="left").pack(fill="x", pady=(0, 6))

        canvas = tk.Canvas(win, bg=SURFACE, highlightthickness=0)
        holder = tk.Frame(canvas, bg=SURFACE)
        holder.bind("<Configure>",
                    lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=holder, anchor="nw", width=410)
        canvas.pack(fill="both", expand=True, padx=(14, 2), pady=(0, 10))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(
            -1 if e.delta > 0 else 1, "units"))
        win.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"),
                 add="+")

        chosen = {p["key"]: bool(p["enabled"]) for p in projects}

        def flip(key, on):
            chosen[key] = on
            self.push("/projects", {"keys": [k for k, v in chosen.items() if v]})

        for project in projects:
            name = project["name"] + ("" if project["exists"]
                                      else f"  {t('projects.missing')}")
            self.check_row(holder, name, project["enabled"],
                           lambda on, k=project["key"]: flip(k, on),
                           dim=not project["exists"]).pack(fill="x", pady=1)
            tk.Label(holder, text=f"      {project['root']}", font=self.small,
                     bg=SURFACE, fg=MUTED, anchor="w").pack(fill="x")

    def trace(self, line):
        try:
            with open(os.path.join(ROOT, "widget.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + line + "\n")
        except OSError:
            pass

    def report_callback_exception(self, exc, value, tb):
        """A frozen build has no console, so a failing callback looks exactly
        like nothing happening. Write it down instead."""
        import traceback
        text = "".join(traceback.format_exception(exc, value, tb))
        try:
            with open(os.path.join(ROOT, "widget.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + text + "\n")
        except OSError:
            pass

    def offer_setup(self):
        """Open the setup screen once, on an install that is not linked yet."""
        state = self.request("/telegram")
        if state is None:
            return self.root.after(4000, self.offer_setup)   # daemon still coming up
        if state.get("has_token") and state.get("chat_id"):
            return
        self.open_setup()

    def open_setup(self):
        """Everything needed to go from a fresh install to a working bridge:
        the token field, what the daemon makes of it, and the steps."""
        state = self.request("/telegram") or {}
        win = self.make_card(t("setup.title"), width=520)

        status = tk.Label(win, font=self.small, bg=SURFACE, fg=MUTED,
                          anchor="w", padx=14, justify="left", wraplength=480)
        status.pack(fill="x", pady=(0, 8))

        row = tk.Frame(win, bg=SURFACE)
        row.pack(fill="x", padx=14)
        tk.Label(row, text=t("setup.token.label"), font=self.small, bg=SURFACE,
                 fg=SECONDARY, anchor="w").pack(side="left")
        entry = tk.Entry(row, font=self.small, bg=RAISED, fg=PRIMARY,
                         insertbackground=PRIMARY, relief="flat",
                         highlightthickness=1, highlightbackground=EDGE)
        entry.pack(side="left", fill="x", expand=True, padx=(8, 0), ipady=3)

        def show(state):
            if state.get("username"):
                status.configure(text=t("setup.ok", username=state["username"]),
                                 fg=GOOD)
            elif state.get("has_token"):
                status.configure(text=t("setup.token.saved",
                                        hint=state.get("token_hint", "")), fg=SECONDARY)
            else:
                status.configure(text=t("setup.token.none"), fg=WARNING)
            group.configure(
                text=t("setup.group.bound", topics=state.get("topics", 0))
                if state.get("chat_id") else t("setup.group.waiting"),
                fg=GOOD if state.get("chat_id") else MUTED)

        def save():
            entry.configure(state="disabled")
            answer = self.request("/telegram", {"token": entry.get()}) or {}
            entry.configure(state="normal")
            if answer.get("ok"):
                entry.delete(0, "end")       # accepted: no need to keep it on screen
                show(answer)
            else:
                status.configure(text=answer.get("error") or t("widget.dead.settings"),
                                 fg=CRITICAL)

        button = tk.Label(win, text=t("setup.save"), font=self.small, bg=RAISED,
                          fg=PRIMARY, cursor="hand2", padx=12, pady=5)
        button.pack(anchor="w", padx=14, pady=(8, 4))
        button.bind("<Button-1>", lambda e: save())
        button.bind("<Enter>", lambda e: button.configure(bg=RAISED_HI))
        button.bind("<Leave>", lambda e: button.configure(bg=RAISED))
        entry.bind("<Return>", lambda e: save())

        group = tk.Label(win, font=self.small, bg=SURFACE, fg=MUTED, anchor="w",
                         padx=14, justify="left", wraplength=480)
        group.pack(fill="x", pady=(0, 6))

        tk.Frame(win, height=1, bg=HAIRLINE).pack(fill="x", padx=14, pady=6)

        # -- hooks: the other half of a working install --------------------
        hooks_row = tk.Frame(win, bg=SURFACE)
        hooks_row.pack(fill="x", padx=14, pady=(2, 6))
        hooks_label = tk.Label(hooks_row, font=self.small, bg=SURFACE, fg=MUTED,
                               anchor="w", justify="left", wraplength=340)
        hooks_label.pack(side="left", fill="x", expand=True)
        hooks_button = tk.Label(hooks_row, text=t("setup.hooks.install"),
                                font=self.small, bg=RAISED, fg=PRIMARY,
                                cursor="hand2", padx=12, pady=5)
        hooks_button.pack(side="right")
        hooks_button.bind("<Enter>", lambda e: hooks_button.configure(bg=RAISED_HI))
        hooks_button.bind("<Leave>", lambda e: hooks_button.configure(bg=RAISED))

        def show_hooks(state):
            if state is None:
                return hooks_label.configure(text=t("widget.dead.settings"),
                                             fg=CRITICAL)
            if state.get("ready"):
                text, colour = t("setup.hooks.ready"), GOOD
            else:
                text, colour = t("setup.hooks.missing"), WARNING
            if state.get("statusline_foreign"):
                text += " " + t("setup.hooks.statusline_foreign")
            hooks_label.configure(text=text, fg=colour)

        hooks_button.bind("<Button-1>",
                          lambda e: show_hooks(self.request("/hooks", {"enable": True})))
        show_hooks(self.request("/hooks"))

        tk.Label(win, text=t("setup.steps.title"), font=self.bold, bg=SURFACE,
                 fg=PRIMARY, anchor="w", padx=14).pack(fill="x")
        for step in ("setup.step1", "setup.step2", "setup.step3",
                     "setup.step4", "setup.step5", "setup.step6"):
            tk.Label(win, text=t(step), font=self.small, bg=SURFACE,
                     fg=SECONDARY, anchor="w", padx=14, pady=2, justify="left",
                     wraplength=480).pack(fill="x")

        link = tk.Label(win, text=t("setup.open.botfather"), font=self.small,
                        bg=SURFACE, fg=ACCENT, cursor="hand2", anchor="w", padx=14)
        link.pack(fill="x", pady=(6, 4))
        link.bind("<Button-1>", lambda e: webbrowser.open("https://t.me/BotFather"))

        build = self.request("/version") or {}
        if build.get("version"):
            # Installed builds keep themselves current; from source they do not,
            # and saying so beats letting someone wait for an update forever.
            note = t("setup.version", version=build["version"])
            if build.get("frozen"):
                note += " · " + t("setup.updates.on" if build.get("enabled")
                                  else "setup.updates.off")
            else:
                note += " · " + t("setup.updates.source")
            tk.Label(win, text=note, font=self.small, bg=SURFACE, fg=MUTED,
                     anchor="w", padx=14).pack(fill="x", pady=(0, 10))

        show(state)
        win.geometry("")        # shrink to whatever the steps needed
        entry.focus_set()

    def open_alarm(self):
        """How long a waiting session sleeps before checking back.

        A number rather than a switch, so it gets a box of its own instead of
        a row among the things that are merely on or off. Zero turns it off,
        which is the same answer as the switch in Features.
        """
        snapshot = self.request("/mode") or {}
        alarm = snapshot.get("alarm") or {}
        win = self.make_card(t("alarm.title"), width=420)
        tk.Label(win, text=t("alarm.hint"), font=self.small, bg=SURFACE,
                 fg=MUTED, anchor="w", padx=14, justify="left",
                 wraplength=380).pack(fill="x", pady=(0, 8))

        row = tk.Frame(win, bg=SURFACE)
        row.pack(fill="x", padx=14)
        tk.Label(row, text=t("alarm.minutes"), font=self.small, bg=SURFACE,
                 fg=SECONDARY, anchor="w").pack(side="left")
        entry = tk.Entry(row, font=self.small, bg=RAISED, fg=PRIMARY,
                         insertbackground=PRIMARY, relief="flat", width=6,
                         highlightthickness=1, highlightbackground=EDGE)
        entry.pack(side="left", padx=(8, 0), ipady=3)
        entry.insert(0, str(alarm.get("minutes", 15))
                     if alarm.get("enabled") else "0")
        note = tk.Label(win, font=self.small, bg=SURFACE, fg=MUTED, anchor="w",
                        padx=14, justify="left", wraplength=380)
        note.pack(fill="x", pady=(6, 0))

        def apply():
            try:
                minutes = max(0, min(240, int(entry.get().strip() or 0)))
            except ValueError:
                note.configure(text=t("alarm.bad"), fg=WARNING)
                return
            self.request("/settings", {"settings": {"wake_alarm.minutes": minutes}})
            note.configure(text=t("alarm.off") if minutes == 0
                           else t("alarm.on", minutes=minutes), fg=GOOD)

        button = tk.Label(win, text=t("alarm.apply"), font=self.small, bg=RAISED,
                          fg=PRIMARY, cursor="hand2", padx=12, pady=5)
        button.pack(anchor="w", padx=14, pady=(10, 6))
        button.bind("<Button-1>", lambda e: apply())
        button.bind("<Enter>", lambda e: button.configure(bg=RAISED_HI))
        button.bind("<Leave>", lambda e: button.configure(bg=RAISED))
        entry.bind("<Return>", lambda e: apply())
        win.geometry("")        # shrink to fit

    def open_settings(self):
        snapshot = self.request("/settings")
        if snapshot is None:
            return self.paint(DEAD, t("widget.dead.settings"))
        toggles = snapshot.get("settings") or []
        win = self.make_card(t("card.settings"))
        tk.Label(win, text=t("card.settings.hint"),
                 font=self.small, bg=SURFACE, fg=MUTED, anchor="w",
                 padx=14).pack(fill="x", pady=(0, 6))
        for toggle in toggles:
            self.check_row(win, toggle["label"], toggle["enabled"],
                           lambda on, p=toggle["path"]: self.push(
                               "/settings", {"settings": {p: on}})
                           ).pack(fill="x", padx=14, pady=1)
        tk.Frame(win, height=6, bg=SURFACE).pack()
        win.geometry("")        # shrink to fit the rows

    # -- rendering -------------------------------------------------------

    def apply(self, snapshot):
        if snapshot is None:
            self.alive = False
            self.paint(DEAD, t("widget.dead.info"))
            return
        self.alive = True
        away = bool(snapshot.get("away"))
        if not self.busy:
            self.away_state = away
        theme = AWAY if away else AT_PC
        sessions = snapshot.get("sessions") or []
        waiting = snapshot.get("waiting") or []
        queued = snapshot.get("queued") or 0

        names = ", ".join(sessions) if sessions else t("status.none")
        info = [t("widget.sessions", names=names)]
        if waiting:
            kinds = {"ask": t("widget.kind.ask"), "stop": t("widget.kind.stop")}
            info.append(t("widget.waiting", kinds=", ".join(
                kinds.get(w.get("kind"), w.get("kind", "?")) for w in waiting)))
        if queued:
            info.append(t("widget.queued", n=queued))
        self.show_alarm(snapshot.get("alarm") or {})
        # One line on the card; the full picture lives in the hover tip,
        # which drops the "Sessions:" prefix — it is plainly the session list.
        line = " · ".join(info)
        if len(line) > 48:
            line = line[:47].rstrip(" ,·") + "…"
        self.paint(theme, line, "\n".join([names] + info[1:]))
        self.auto_presence(away)

    def paint(self, theme, info, full=None):
        self.theme = theme
        self.paint_toggle()
        self.info_label.configure(text=info)
        self.info_full = full or info
        self.toggle_tip.refresh()
        self.info_tip.refresh()

    def set_alpha(self):
        self.root.attributes("-alpha", self.alpha_var.get())
        for punch in self.punches:
            punch["win"].attributes("-alpha", self.alpha_var.get())
        if self.edge:
            self.edge.set_alpha(self.alpha_var.get())
        self.save_pos()

    def apply_click_through(self):
        """Mouse-transparent card, minus the presence capsule.

        WS_EX_TRANSPARENT is a whole-window style — there is no way to punch a
        hole in it. So the capsule gets a second, solid window of its own,
        parked exactly over the real one and tracking it.
        """
        on = bool(self.through_var.get())
        try:
            click_through(self.root, on)
        except (OSError, AttributeError, tk.TclError):
            return          # not Windows, or no window yet: leave it solid
        # A tip open at that moment would hang there: <Leave> never arrives
        # once the card stops receiving mouse events.
        self.info_tip.hide()
        self.toggle_tip.hide()
        self.build_punch() if on else self.drop_punch()
        # The card's size did not change, so <Configure> will not fire: cut or
        # restore the hole explicitly.
        self.shape_card()
        self.save_pos()

    def build_punch(self):
        """One solid stand-in per control that has to stay clickable."""
        if self.punches:
            return self.place_punch()
        self.punches = [self.make_punch(self.toggle, self.fill_capsule),
                        self.make_punch(self.gear, self.fill_gear)]
        self.place_punch()

    def make_punch(self, source, fill):
        win = tk.Toplevel(self.root)
        # Its own title: it inherits the card's otherwise, and then anything
        # looking the widget up by name finds this sliver instead.
        win.title("Claude ↔ Telegram control")
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", self.alpha_var.get())
        win.configure(bg=self.surface)
        return {"win": win, "source": source, "body": fill(win), "box": None}

    def fill_capsule(self, win):
        canvas = tk.Canvas(win, bg=self.surface, highlightthickness=0,
                           cursor="hand2")
        canvas.pack(fill="both", expand=True)
        canvas.bind("<Button-1>", lambda e: self.on_toggle())
        canvas.bind("<Enter>", lambda e: self.set_hover(True))
        canvas.bind("<Leave>", lambda e: self.set_hover(False))
        canvas.bind("<Button-3>", self.open_menu)
        Tooltip(canvas, lambda: t(self.theme["label"]), self.small)
        return canvas

    def fill_gear(self, win):
        label = tk.Label(win, text="⚙", font=self.gear_font, bg=self.surface,
                         fg=MUTED, cursor="hand2")
        label.pack(fill="both", expand=True)
        label.bind("<Button-1>", self.open_menu)
        label.bind("<Button-3>", self.open_menu)
        label.bind("<Enter>", lambda e: label.configure(fg=PRIMARY))
        label.bind("<Leave>", lambda e: label.configure(fg=MUTED))
        return label

    def drop_punch(self):
        for punch in self.punches:
            try:
                punch["win"].destroy()
            except tk.TclError:
                pass
        self.punches = []

    def place_punch(self):
        """Keep every stand-in exactly over the control it stands in for."""
        if not self.punches:
            return
        moved = False
        for punch in self.punches:
            try:
                if not self.root.winfo_viewable():
                    punch["win"].withdraw()
                    continue
                source = punch["source"]
                # The control's own screen coordinates. Adding winfo_x() to the
                # card's origin drops the padding of every frame in between,
                # which parked the stand-in up and left of the real control.
                box = (max(source.winfo_width(), 1), max(source.winfo_height(), 1),
                       source.winfo_rootx(), source.winfo_rooty())
                if box != punch["box"]:
                    punch["box"] = box
                    moved = True
                # Only when actually hidden: deiconify on a visible window
                # raises it, and it would climb over an open card.
                if not punch["win"].winfo_viewable():
                    punch["win"].deiconify()
                punch["win"].geometry("%dx%d+%d+%d" % box)
                punch["win"].configure(bg=self.surface)
                punch["body"].configure(bg=self.surface)
                if isinstance(punch["body"], tk.Canvas):
                    self.paint_capsule(punch["body"])
            except tk.TclError:
                continue
        if moved:
            self.shape_card()           # the holes follow the controls
        self.raise_overlays()

    def raise_overlays(self):
        """Layering, bottom to top: card, then the buttons, then menus and
        windows. Without this the stand-ins float over the Projects/Features
        window that was opened on top of them."""
        for win in list(self.overlays):
            try:
                if win.winfo_exists():
                    lift_window(win)
                else:
                    self.overlays.remove(win)
            except tk.TclError:
                self.overlays.remove(win)
        for win in self.pop.wins:
            try:
                lift_window(win)
            except tk.TclError:
                pass

    def toggle_fable(self):
        self.fable_shown.set(0 if self.fable_shown.get() else 1)
        self.layout_limits()
        self.save_pos()

    def toggle_limits(self):
        self.limits_shown.set(0 if self.limits_shown.get() else 1)
        self.layout_limits()
        self.save_pos()

    def show_alarm(self, alarm):
        """Its own row while an alarm is set, and no row at all when none is."""
        wanted = bool(alarm.get("enabled"))
        shown = bool(self.alarm_label.winfo_ismapped())
        if wanted:
            self.alarm_label.configure(
                text=t("widget.alarm", minutes=alarm.get("minutes", 15)))
        if wanted == shown:
            return
        if wanted:
            self.alarm_label.pack(fill="x", pady=(4, 0), before=self.limits_bar)
        else:
            self.alarm_label.pack_forget()
        self.root.geometry("")                  # the card grew or shrank
        self.root.after(50, self.place_punch)   # the capsule may have moved

    def layout_limits(self):
        """Fold the meters — and the Fable row inside them — in or out.

        Collapsing the block takes the Fable tab with it: it switches a row
        that is no longer on screen, and leaving it behind reads as a live
        control that does nothing.

        A nearly spent Fable window overrides both folds without touching the
        saved preference: hiding the one number that has run out is the one
        thing the card must not do. Folding it away again works as soon as the
        window resets.
        """
        limits = bool(self.limits_shown.get()) or self.fable_alarm
        fable = bool(self.fable_shown.get()) or self.fable_alarm
        row = self.meters["fable"]
        for part in ("caption", "bar", "pct"):
            row[part].grid() if (limits and fable) else row[part].grid_remove()
        if limits:
            # `after` is required: re-packing appends to the end of the
            # parent's list, which floated the session line above the meters
            # instead of leaving it at the bottom of the card.
            self.meters_frame.pack(fill="x", padx=14, pady=(5, 5),
                                   after=self.limits_bar)
            self.fable_tab.pack(side="right")
        else:
            self.meters_frame.pack_forget()
            self.fable_tab.pack_forget()
        self.paint_tabs()
        self.root.geometry("")      # shrink or grow the card to fit
        self.root.after(50, self.place_punch)   # the capsule may have moved

    def paint_tabs(self):
        """Lit when its section is out, dimmed when folded away: one glance
        says both what the control is and whether it is showing.

        Once the Fable window is filling up the tab stops reporting the fold
        and starts reporting the limit — dimming a warning to say "this row is
        collapsed" would bury the only thing worth seeing.
        """
        color = SEG_COLORS["fable"]
        pct = self.fable_pct
        if pct is not None and pct >= ALERT_PERCENT:
            fable_fg = CRITICAL
        elif pct is not None and pct >= 70:
            fable_fg = WARNING
        elif self.fable_shown.get():
            fable_fg = color
        else:
            fable_fg = blend(color, self.surface, 0.55)
        self.fable_tab.render("Fable", fable_fg, self.surface)
        self.limits_tab.render(t("widget.limits"),
                               SECONDARY if self.limits_shown.get() else MUTED,
                               self.surface)

    @classmethod
    def walk(cls, widget):
        for child in widget.winfo_children():
            if isinstance(child, tk.Toplevel):
                continue        # cards, menus and tips own their background
            yield child
            yield from cls.walk(child)

    def alert_for(self, shown):
        """Which alarm the card should wear.

        Either weekly window wins over the 5-hour one — Fable's is weekly too,
        resets on the same date and will not refill by itself, so a spent
        Fable pool is exactly as final as a spent shared one.
        """
        weekly = max(shown.get("seven_day", 0), shown.get("fable", 0))
        if weekly >= ALERT_PERCENT:
            return ALERT_WEEK
        if shown.get("five_hour", 0) >= ALERT_PERCENT:
            return ALERT_SESSION
        return SURFACE

    def set_surface(self, color):
        """Repaint every part that carries the window background.

        Matching on the previous colour rather than listing the widgets keeps
        this honest when the card grows a row: anything that was background
        stays background.
        """
        if color == self.surface:
            return
        previous, self.surface = self.surface, color
        self.root.configure(bg=color)
        for widget in self.walk(self.root):
            try:
                if str(widget.cget("bg")) == previous:
                    widget.configure(bg=color)
            except tk.TclError:
                pass            # separators and anything without a bg
        self.paint_toggle()
        self.paint_tabs()
        self.place_punch()      # the stand-in wears the same background

    @staticmethod
    def window_for(limits, key):
        """Fable is scoped to a weekly window only — it draws from the same
        5-hour pool as everything else, so there is nothing else to unpack."""
        if key == "fable":
            section = limits.get("fable")
            return (section or {}).get("seven_day") or {}
        return limits.get(key) or {}

    def apply_usage(self, data):
        self.last_usage = data
        limits = (data or {}).get("rate_limits") or {}
        shown = {}
        for key, widgets in self.meters.items():
            window = self.window_for(limits, key)
            used = window.get("used_percentage")
            if used is None:
                widgets["pct"].configure(text="—")
                self.tips[key] = t(NO_FABLE_TIP if key == "fable" and limits
                                   else NO_DATA_TIP)
                continue
            # The reset time lives in the tooltip: on the card it cost a line
            # per meter and pushed the numbers apart.
            moment = usage.when(window.get("resets_at"))
            widgets["pct"].configure(text=f"{used:.0f}%")
            self.tips[key] = f"{used:.0f}% · {moment}" if moment else f"{used:.0f}%"
            shown[key] = used
        # Background first: the meter track is mixed into whatever the card
        # is wearing, so drawing the bars before it would leave them tinted
        # for the old colour.
        self.set_surface(self.alert_for(shown))
        self.follow_fable(shown.get("fable"))
        for key, widgets in self.meters.items():
            self.draw_meter(widgets["bar"], shown.get(key), widgets["ramp"])
        self.show_session_in_tray(shown.get("five_hour"))

    def follow_fable(self, percent):
        """Keep the tab colour and the forced unfold in step with the pool."""
        alarm = percent is not None and percent >= ALERT_PERCENT
        changed = alarm != self.fable_alarm
        self.fable_pct, self.fable_alarm = percent, alarm
        if changed:
            self.layout_limits()        # repaints the tabs on its way out
        else:
            self.paint_tabs()

    def show_session_in_tray(self, percent):
        """The session percentage on the tray icon, so the number is readable
        with the card hidden. Redrawn only when the rounded value moves."""
        if percent is None:
            return
        rounded = int(round(percent))
        if rounded == self.tray_percent:
            return
        self.tray_percent = rounded
        # The same ramp as the session meter, so the icon and the bar never
        # disagree about how bad things are.
        self.tray.set_percent(rounded, session_color(percent))

    def draw_meter(self, canvas, percent, ramp=None):
        """Capsule meter: the ramp picks the fill, track = its own quiet step."""
        # Resolved here, not as a default: the ramps are defined below the
        # class, so a default argument would be evaluated before they exist.
        ramp = ramp or severity
        canvas.delete("all")
        canvas.update_idletasks()
        width = max(canvas.winfo_width(), 60)
        fill = ramp(percent) if percent is not None else ACCENT
        self.capsule(canvas, 0, width, blend(fill, self.surface, 0.78))
        if percent is not None and percent > 0:
            share = max(0.0, min(100.0, percent)) / 100.0
            self.capsule(canvas, 0, max(BAR_H, round(width * share)), fill)

    @staticmethod
    def capsule(canvas, x0, x1, color):
        r = BAR_H / 2
        canvas.create_oval(x0, 0, x0 + BAR_H, BAR_H, fill=color, outline="")
        canvas.create_oval(x1 - BAR_H, 0, x1, BAR_H, fill=color, outline="")
        canvas.create_rectangle(x0 + r, 0, x1 - r, BAR_H, fill=color, outline="")

    # -- window position --------------------------------------------------

    @staticmethod
    def load_saved():
        # utf-8-sig, not utf-8: an editor (or PowerShell's Set-Content) that
        # leaves a BOM would otherwise make the whole file unreadable, and the
        # widget would silently come up with every setting reset.
        try:
            with open(POS_FILE, encoding="utf-8-sig") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_pos(self):
        try:
            with open(POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": self.root.winfo_x(), "y": self.root.winfo_y(),
                           "alpha": self.alpha_var.get(),
                           "auto_away_seconds": self.auto_var.get(),
                           "fable_shown": self.fable_shown.get(),
                           "limits_shown": self.limits_shown.get(),
                           "click_through": self.through_var.get()}, f)
        except OSError:
            pass

    def close(self):
        self.save_pos()
        self.drop_punch()
        if self.edge:
            self.edge.destroy()
            self.edge = None
        self.tray.remove()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def load_cfg():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def blend(color, into, amount):
    """Mix `color` toward `into`; the meter track is the fill's own quiet step."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(into[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(
        round(x + (y - x) * amount) for x, y in zip(a, b))




def usage_summary(data):
    """One line of whatever the limits currently say, for the settings card."""
    if not data:
        return t("setup.usage.none")
    limits = (data or {}).get("rate_limits") or {}
    parts = []
    for key in ("five_hour", "seven_day"):
        label = f"widget.meter.{key}"
        used = (limits.get(key) or {}).get("used_percentage")
        if used is not None:
            parts.append(f"{t(label)} {used:.0f}%")
    fable = ((limits.get("fable") or {}).get("seven_day") or {}).get("used_percentage")
    if fable is not None:
        parts.append(f"Fable {fable:.0f}%")
    if not parts:
        return t("setup.usage.none")
    return " · ".join(parts) + " · " + usage.when_captured(data)


def severity(percent, calm=ACCENT):
    """Coarse ramp for the weekly windows: they move slowly enough that two
    steps say everything."""
    if percent >= ALERT_PERCENT:
        return CRITICAL
    if percent >= 70:
        return WARNING
    return calm


def session_color(percent, calm=ACCENT):
    """Fine ramp for the 5-hour window: yellow at 60, through orange, red at 90."""
    for threshold, color in SESSION_STEPS:
        if percent >= threshold:
            return color
    return calm


def raise_existing():
    """Bring the copy that is already running to the front.

    A second launch — from the Start menu while autostart already ran, or an
    installed build next to one started from source — should look like the app
    responding, not like nothing happening.
    """
    user32 = ctypes.windll.user32
    for title in ("Claude ↔ Telegram", "Claude ↔ Telegram control"):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
    return False


if __name__ == "__main__":
    # Held for the life of the process: releasing it would let a second copy in.
    _claim = paths.single_instance("ClaudeTelegramWidget")
    if _claim is None:
        raise_existing()
        sys.exit(0)
    Widget().run()
