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
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import font as tkfont

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from claudetg import usage  # noqa: E402  (needs ROOT on the path)

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

AT_PC = {"dot": GOOD, "button": "Я за ПК", "label": "за ПК — только журнал"}
AWAY = {"dot": WARNING, "button": "Не за ПК", "label": "управление в Telegram"}
DEAD = {"dot": CRITICAL, "button": "Мост offline",
        "label": "демон не отвечает — кликни, подниму"}

BAR_H = 8                  # meter thickness; r = h/2 gives the 4px rounded end
NO_DATA_TIP = "нет данных от статус-строки — перезапусти окно VSCode"
NO_FABLE_TIP = "нет данных по Fable-лимитам — возможно, статус-строка их не присылает"
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


def start_card_drag(event, win):
    win._drag = (event.x_root - win.winfo_x(), event.y_root - win.winfo_y())


def card_drag(event, win):
    dx, dy = getattr(win, "_drag", (0, 0))
    win.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")


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

    def __init__(self, on_event, on_key):
        super().__init__(daemon=True)
        self.on_event = on_event
        self.on_key = on_key
        self.hwnd = None
        self.hicon = None

    def run(self):
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

        self.proc = WNDPROC(wnd_proc)         # keep alive or they are GC'd
        self.kb = HOOKPROC(kb_proc)

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
        wc.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, None, 0, 0, 0,
                                           0, 0, None, None, wc.hInstance, None)
        self.hicon = user32.LoadImageW(None, ensure_icon(), 1, 0, 0, 0x10)
        ctypes.windll.shell32.Shell_NotifyIconW(0, ctypes.byref(self._nid()))
        self.hook = user32.SetWindowsHookExW(13, self.kb, None, 0)  # WH_KEYBOARD_LL

        msg = ctypes.create_string_buffer(48)
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

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
        self.sub_owner = None      # the row whose submenu is currently open

    def popup(self, x, y, items):
        self.close()
        self.wins = [self._build(items, x, y)]

    def _build(self, items, x, y):
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=SURFACE, highlightthickness=1, highlightbackground=EDGE)
        win.bind("<Enter>", lambda e: self._cancel_hide(), add="+")
        win.bind("<Leave>", lambda e: self._schedule_hide(), add="+")
        for item in items:
            self._row(win, item)
        win.geometry(f"+{x}+{y}")
        win.after(10, lambda: round_window(win, 10))
        return win

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
            # Plain rows leave an open submenu alone: the pointer's diagonal
            # path toward it inevitably grazes the neighbours, and closing on
            # every graze made the submenu flicker away mid-flight.
            if arrow is None:
                return
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
            w.bind("<Leave>", lambda e: paint(False))
            w.bind("<Button-1>", click)

    def _close_from(self, win):
        """Closing a submenu chain when the pointer moves to another row."""
        index = self.wins.index(win) if win in self.wins else -1
        for extra in self.wins[index + 1:]:
            extra.destroy()
        self.wins = self.wins[:index + 1]

    def _schedule_hide(self):
        self._cancel_hide()
        self.hide_job = self.root.after(600, self.close)

    def _cancel_hide(self):
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
            self.hide_job = None

    def close(self):
        self._cancel_hide()
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
        host = cfg.get("host", "127.0.0.1")
        port = cfg.get("port", 8787)
        self.base = f"http://{host}:{port}"
        self.max_age = (cfg.get("usage_report") or {}).get("max_age_seconds", 3600)
        self.alive = False
        self.busy = False
        self.away_state = False
        self.theme = AT_PC
        self.hover = False
        self.tips = {}
        self.drag = None
        self.last_key_ts = 0.0

        saved = self.load_saved()
        self.root = tk.Tk()
        self.root.title("Claude ↔ Telegram")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=SURFACE, highlightthickness=1,
                            highlightbackground=EDGE, highlightcolor=EDGE)
        self.root.geometry(f"+{int(saved.get('x', 40))}+{int(saved.get('y', 40))}")
        self.alpha_var = tk.DoubleVar(value=float(saved.get("alpha", 0.8)))
        self.root.attributes("-alpha", self.alpha_var.get())
        self.auto_var = tk.IntVar(value=int(saved.get("auto_away_seconds", 300)))
        self.auto_away_active = False
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

        self.toggle_tip = Tooltip(self.toggle, lambda: self.theme["label"], small)

        tk.Frame(self.root, height=1, bg=HAIRLINE).pack(fill="x", padx=10,
                                                        pady=(7, 0))

        # -- limits header: which pool the meters show --------------------
        self.limits_view = tk.StringVar(value=saved.get("limits_view", "standard"))
        self.last_usage = None
        limits_bar = tk.Frame(self.root, bg=SURFACE)
        limits_bar.pack(fill="x", padx=14, pady=(6, 0))
        tk.Label(limits_bar, text="Лимиты", font=small, bg=SURFACE, fg=MUTED,
                 anchor="w").pack(side="left")
        self.seg = {}
        for view, caption in (("fable", "Fable"), ("standard", "Стандарт")):
            lab = tk.Label(limits_bar, text=caption, font=small, bg=SURFACE,
                           fg=MUTED, cursor="hand2", padx=5)
            lab.pack(side="right")
            lab.bind("<Button-1>",
                     lambda e, v=view: (self.set_limits_view(v), "break")[1])
            self.seg[view] = lab
        self.limits_bar = limits_bar
        self.paint_seg()

        # -- usage meters (5h window / weekly window) ---------------------
        meters = tk.Frame(self.root, bg=SURFACE)
        meters.pack(fill="x", padx=14, pady=(5, 5))
        meters.columnconfigure(1, weight=1)
        self.meters = {}
        for row, (key, label) in enumerate((("five_hour", "Сессия"),
                                            ("seven_day", "Неделя"))):
            tk.Label(meters, text=label, font=small, bg=SURFACE, fg=SECONDARY,
                     anchor="w", width=8).grid(row=row * 2, column=0, sticky="w",
                                               pady=2)
            bar = tk.Canvas(meters, width=170, height=BAR_H, bg=SURFACE,
                            highlightthickness=0)
            bar.grid(row=row * 2, column=1, sticky="ew", padx=(2, 8), pady=2)
            pct = tk.Label(meters, text="—", font=value, bg=SURFACE, fg=PRIMARY,
                           anchor="e", width=5)
            pct.grid(row=row * 2, column=2, sticky="e", pady=2)
            reset = tk.Label(meters, text="", font=small, bg=SURFACE, fg=MUTED,
                             anchor="w")
            reset.grid(row=row * 2 + 1, column=1, columnspan=2, sticky="w",
                       padx=(2, 0), pady=(0, 2))
            reset.grid_remove()     # appears only when there is data to show
            self.meters[key] = {"bar": bar, "pct": pct, "reset": reset}
            self.tips[key] = NO_DATA_TIP
            Tooltip(bar, lambda k=key: self.tips[k], small)

        tk.Frame(self.root, height=1, bg=HAIRLINE).pack(fill="x", padx=10)
        self.info_full = ""
        self.info_label = tk.Label(self.root, text="", font=small, bg=SURFACE,
                                   fg=MUTED, anchor="w", padx=14)
        self.info_label.pack(fill="x", pady=(4, 6))
        self.info_tip = Tooltip(self.info_label, lambda: self.info_full, small)

        # -- borderless plumbing: menu everywhere, drag on passive parts --
        self.pop = PopupMenu(self.root, small)
        for area in (self.root, top, self.info_label, meters, limits_bar,
                     *[w for m in self.meters.values() for w in m.values()]):
            area.bind("<Button-3>", self.open_menu)
            area.bind("<Button-1>", self.start_drag, add="+")
            area.bind("<B1-Motion>", self.on_drag, add="+")
            area.bind("<ButtonRelease-1>", self.end_drag, add="+")

        self._region = (0, 0)
        self.root.bind("<Configure>", self.round_corners)
        self.tray_event = None
        self.tray = Tray(self.on_tray, self.on_key)
        self.tray.start()
        self.root.after(150, self.tray_pump)
        self.paint_toggle()
        self.poll()

    # -- menu -------------------------------------------------------------

    def menu_items(self):
        alpha = [("radio", f"{100 - int(opacity * 100)}%", self.alpha_var,
                  opacity, self.set_alpha)
                 for opacity in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5)]
        auto = [("radio", label, self.auto_var, seconds, self.save_pos)
                for seconds, label in ((0, "Выкл"), (60, "1 мин"), (120, "2 мин"),
                                       (300, "5 мин"), (600, "10 мин"),
                                       (900, "15 мин"))]
        return [
            ("cmd", "Проекты…", self.open_projects),
            ("cmd", "Функции…", self.open_settings),
            ("sub", "Прозрачность", alpha),
            ("sub", "Авто «не за ПК»", auto),
            ("sep",),
            ("cmd", "Скрыть в трей", self.root.withdraw),
            ("cmd", "Закрыть виджет", self.close),
        ]

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

    def toggle_visible(self):
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            # Some WMs drop these on deiconify; reassert both.
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
        else:
            self.root.withdraw()

    # -- presence capsule ------------------------------------------------

    def set_hover(self, on):
        self.hover = on
        self.paint_toggle()

    def paint_toggle(self):
        c = self.toggle
        c.delete("all")
        w, h = max(c.winfo_width(), 60), 30
        r = h // 2
        fill = RAISED_HI if self.hover else RAISED
        c.create_oval(0, 0, 2 * r, h, fill=fill, outline="")
        c.create_oval(w - 2 * r, 0, w, h, fill=fill, outline="")
        c.create_rectangle(r, 0, w - r, h, fill=fill, outline="")
        c.create_oval(14, 11, 22, 19, fill=self.theme["dot"], outline="")
        c.create_text(32, 15, text=self.theme["button"], font=self.bold,
                      fill=PRIMARY, anchor="w")

    # -- borderless window plumbing --------------------------------------

    def round_corners(self, _event=None):
        w, h = self.root.winfo_width(), self.root.winfo_height()
        if w <= 1 or (w, h) == self._region:
            return
        self._region = (w, h)
        round_window(self.root, 22)

    def start_drag(self, event):
        self.drag = (event.x_root - self.root.winfo_x(),
                     event.y_root - self.root.winfo_y())

    def on_drag(self, event):
        if self.drag:
            dx, dy = self.drag
            self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

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
        self.root.after(POLL_MS, self.poll)

    def _poll_worker(self):
        snapshot = self.request("/mode")
        limits = usage.load(max_age=self.max_age)
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
        """Idle keyboard+mouse = gone: flip to away. Only a KEY PRESS flips
        back — a nudged mouse or a cat on the desk should not yank a session
        out of Telegram. Manual switches are never undone automatically.
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
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0)
        try:
            subprocess.Popen([sys.executable, "-m", "claudetg.daemon"], cwd=ROOT,
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
        win.attributes("-alpha", max(self.alpha_var.get(), 0.9))
        win.configure(bg=SURFACE, highlightthickness=1, highlightbackground=EDGE)
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
        win.after(10, lambda: round_window(win, 12))
        win.focus_force()
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
            return self.paint(DEAD, "демон не отвечает — список недоступен")
        projects = snapshot.get("projects") or []
        win = self.make_card("Проекты")
        tk.Label(win, text="Отмеченные проекты получают свой топик в Telegram · "
                           "изменения применяются сразу",
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
            name = project["name"] + ("" if project["exists"] else "  (нет на диске)")
            self.check_row(holder, name, project["enabled"],
                           lambda on, k=project["key"]: flip(k, on),
                           dim=not project["exists"]).pack(fill="x", pady=1)
            tk.Label(holder, text=f"      {project['root']}", font=self.small,
                     bg=SURFACE, fg=MUTED, anchor="w").pack(fill="x")

    def open_settings(self):
        snapshot = self.request("/settings")
        if snapshot is None:
            return self.paint(DEAD, "демон не отвечает — настройки недоступны")
        toggles = snapshot.get("settings") or []
        win = self.make_card("Функции")
        tk.Label(win, text="Изменения применяются сразу, без перезапуска",
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
            self.paint(DEAD, "правый клик — меню и закрытие")
            return
        self.alive = True
        away = bool(snapshot.get("away"))
        if not self.busy:
            self.away_state = away
        theme = AWAY if away else AT_PC
        sessions = snapshot.get("sessions") or []
        waiting = snapshot.get("waiting") or []
        queued = snapshot.get("queued") or 0

        names = ", ".join(sessions) if sessions else "нет"
        info = [f"Сессии: {names}"]
        if waiting:
            kinds = {"ask": "опросник", "stop": "жду задачу"}
            info.append("Ждёт ответа: " + ", ".join(
                kinds.get(w.get("kind"), w.get("kind", "?")) for w in waiting))
        if queued:
            info.append(f"В очереди задач: {queued}")
        # One line on the card; the full picture lives in the hover tip,
        # which skips the "Сессии:" prefix — it is plainly the session list.
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
        self.save_pos()

    def set_limits_view(self, view):
        self.limits_view.set(view)
        self.paint_seg()
        self.save_pos()
        self.apply_usage(self.last_usage)

    def paint_seg(self):
        """Each pool keeps its own hue; the inactive one is a dimmed step of
        the same hue, so identity and selection read at the same time."""
        active = self.limits_view.get()
        for view, lab in self.seg.items():
            color = SEG_COLORS[view]
            lab.configure(fg=color if view == active
                          else blend(color, SURFACE, 0.55))

    def window_for(self, limits, key):
        """The standard pool lives at the top level; the Fable pool's real
        shape is unknown until a live sample lands, so probe the plausible
        spots: a nested section or a prefixed key."""
        if self.limits_view.get() == "fable":
            section = limits.get("fable")
            if isinstance(section, dict) and isinstance(section.get(key), dict):
                return section[key]
            for candidate in (f"fable_{key}", f"{key}_fable"):
                if isinstance(limits.get(candidate), dict):
                    return limits[candidate]
            return {}
        return limits.get(key) or {}

    def apply_usage(self, data):
        self.last_usage = data
        limits = (data or {}).get("rate_limits") or {}
        fable_view = self.limits_view.get() == "fable"
        for key, widgets in self.meters.items():
            window = self.window_for(limits, key)
            used = window.get("used_percentage")
            if used is None:
                widgets["pct"].configure(text="—")
                widgets["reset"].grid_remove()
                self.tips[key] = NO_FABLE_TIP if fable_view and limits else NO_DATA_TIP
                self.draw_meter(widgets["bar"], None)
                continue
            moment = usage.when(window.get("resets_at"))
            widgets["pct"].configure(text=f"{used:.0f}%")
            if moment:
                widgets["reset"].configure(text=moment)
                widgets["reset"].grid()
            else:
                widgets["reset"].grid_remove()
            self.tips[key] = f"{used:.0f}% · {moment}" if moment else f"{used:.0f}%"
            self.draw_meter(widgets["bar"], used)

    def draw_meter(self, canvas, percent):
        """Capsule meter: severity fill, track = the fill's own quiet step."""
        canvas.delete("all")
        canvas.update_idletasks()
        width = max(canvas.winfo_width(), 60)
        fill = severity(percent) if percent is not None else ACCENT
        self.capsule(canvas, 0, width, blend(fill, SURFACE, 0.78))
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
        try:
            with open(POS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_pos(self):
        try:
            with open(POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": self.root.winfo_x(), "y": self.root.winfo_y(),
                           "alpha": self.alpha_var.get(),
                           "auto_away_seconds": self.auto_var.get(),
                           "limits_view": self.limits_view.get()}, f)
        except OSError:
            pass

    def close(self):
        self.save_pos()
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


def severity(percent):
    if percent >= 90:
        return CRITICAL
    if percent >= 70:
        return WARNING
    return ACCENT


if __name__ == "__main__":
    Widget().run()
