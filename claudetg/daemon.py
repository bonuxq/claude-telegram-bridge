"""The bridge daemon: owns the Telegram bot, serves hook requests.

One resident process is required because Telegram delivers updates to exactly
one `getUpdates` consumer per token; two VSCode sessions polling the same token
would steal each other's answers with a 409.
"""

import html
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as cfgmod
from . import discover, hooks, i18n, paths, render, status, transcript
from . import updater, usage, version
from .i18n import t
from .tgapi import Bot, TelegramError

LOG_LOCK = threading.Lock()
LOG_PATH = paths.in_app("daemon.log")
LOG_MAX_BYTES = 2_000_000


def log(*parts):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} " + " ".join(str(p) for p in parts)
    with LOG_LOCK:
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass  # started via pythonw: there is no console to print to
        try:
            # Under autostart the file is the only diagnostic there is.
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".1")
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


CHAT_APPS = ("telegram.exe", "telegram desktop.exe", "unigram.exe", "64gram.exe")


def foreground_app():
    """The program in front right now, lower-cased, or None if unknown."""
    try:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(user32.GetForegroundWindow(),
                                        ctypes.byref(pid))
        if not pid.value:
            return None
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # LIMITED_INFO
        if not handle:
            return None
        try:
            size = wintypes.DWORD(260)
            buf = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                       ctypes.byref(size)):
                return None
            return buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return None


def input_idle_seconds():
    """Seconds since the last key press or mouse movement, system-wide.

    None when the question cannot be asked — off Windows, or if the call
    fails — so a caller can tell "nobody has touched it" apart from "no idea",
    and never mistake the second for the first.
    """
    try:
        import ctypes

        class LastInput(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LastInput()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)
    except (OSError, AttributeError, ValueError):
        return None


class Waiter:
    """A hook blocked on the user. Resolved by a Telegram update or a timeout."""

    def __init__(self, kind, session_id, project, payload=None):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.session_id = session_id
        self.project = project
        self.payload = payload or {}
        self.event = threading.Event()
        self.result = None
        self.message_ids = []
        # The thread its question went to, so the answer comes back the same
        # way rather than being derived from the project again.
        self.thread = None

    def resolve(self, result):
        self.result = result
        self.event.set()

    def wait(self, seconds):
        return self.result if self.event.wait(seconds) else None


class Daemon:
    # How far the usage poll will stretch itself when the server keeps
    # refusing, and how many clean polls it takes before it speeds back up.
    USAGE_FLOOR_MAX = 900
    USAGE_CALM = 5

    @classmethod
    def widen_poll(cls, floor, interval, asked):
        """The cadence to fall back to after the server refused a poll.

        Honouring Retry-After alone is not enough: the server answers "wait 60"
        to a poll it refused *because* it runs every 60 seconds, so obeying it
        literally reproduces the refusal on the next tick, forever. Doubling
        the floor walks the poll out to a rate the server tolerates.
        """
        return min(cls.USAGE_FLOOR_MAX, max(asked, (floor or interval) * 2))

    @staticmethod
    def calm_poll(floor, interval):
        """Halve the penalty after a clean run, never below the configured
        interval, so a passing rate limit does not slow the poll down all day."""
        return max(interval, floor // 2)

    def __init__(self, cfg):
        self.cfg = cfg
        self.bot = Bot(cfg["bot_token"])
        self.state = cfgmod.load_state()
        self.state.setdefault("topics", {})
        self.state.setdefault("away", cfg.get("away_default", False))
        self.state.setdefault("offset", None)
        self.state.setdefault("status_message_id", None)
        self.state.pop("slots", None)   # numbered threads, tried and removed
        self.lock = threading.RLock()
        self.tail_lock = threading.Lock()   # one transcript flush at a time
        self.sessions = {}          # session_id -> live session info
        self.waiters = {}           # waiter id -> Waiter
        self.by_topic = {}          # topic id -> waiter id (newest wins)
        self.queue = {}             # project key -> [pending task texts]
        self.queue.update(self.state.get("queue", {}))
        self.spawning = set()       # project keys with a headless agent running

    # -- persistence ----------------------------------------------------

    def persist(self):
        with self.lock:
            self.state["queue"] = self.queue
            cfgmod.save_state(self.state)

    @property
    def away(self):
        return bool(self.state.get("away"))

    # -- topics ---------------------------------------------------------

    def linked(self):
        """Telegram is configured and bound to a group.

        Everything else runs without it: the widget, the rate-limit meters,
        the hooks, the queue. Only the reporting has nowhere to go — and
        attempting it anyway costs a network timeout on every single event.
        """
        return bool(self.cfg.get("bot_token") and self.cfg.get("chat_id"))

    def per_session_topics(self):
        return bool((self.cfg.get("session_topics") or {}).get("enabled"))

    @staticmethod
    def topic_key(project_key, number):
        """Number 1 keeps the project's own key, so a group that has always
        had one topic per project keeps using it when this is switched on."""
        return project_key if number <= 1 else f"{project_key}#{number}"

    def topic_number(self, project_key, session_id):
        """Which numbered thread this chat owns, for as long as it exists.

        Bound to the session id and never handed back. The first attempt at
        this numbered by slot and released the number when the window closed,
        so continuing the same chat in the editor was given whichever number
        happened to be free — a conversation that already had a thread got a
        second one, and which thread was which stopped meaning anything.

        A session id survives closing the editor and reopening the chat: the
        transcript of this very conversation carries one id across three days
        and several restarts. So the same chat comes back to the same topic,
        and only a genuinely new chat is given a new number.
        """
        with self.lock:
            table = self.state.setdefault("session_topics", {}).setdefault(
                project_key, {})
            number = table.get(session_id)
            if number is None:
                number = max(table.values(), default=0) + 1
                table[session_id] = number
        self.persist()
        return number

    def topic_for(self, project_key, project_name, session_id=None):
        """Return the forum topic to write in, creating it on first use.

        One topic per project unless session_topics is on, in which case each
        chat gets its own numbered thread — and keeps it.
        """
        number = 1
        if session_id and self.per_session_topics():
            number = self.topic_number(project_key, session_id)
            project_key = self.topic_key(project_key, number)
        with self.lock:
            existing = self.state["topics"].get(project_key)
        if existing:
            return existing
        title = project_name if number <= 1 else f"{project_name} #{number}"
        try:
            topic = self.bot.create_topic(self.cfg["chat_id"], title[:128])
            tid = topic["message_thread_id"]
        except TelegramError as e:
            log("createForumTopic failed:", e, "- falling back to General")
            tid = None
        with self.lock:
            self.state["topics"][project_key] = tid
        self.persist()
        if tid:
            # A topic without the switch is a topic you cannot hand control
            # over from, and a new one is created exactly when somebody starts
            # working somewhere new.
            self.refresh_topic_switches(only=tid)
        return tid

    def send(self, project_key, project_name, text, markup=None, silent=None,
             thread=None, session=None):
        """Send rendered HTML to the project's topic. Returns message ids.

        `session` picks that chat's own thread when session_topics is on;
        `thread` overrides both, for a reply that must land where it was asked.
        """
        if not self.linked():
            return []       # not set up yet: stay silent instead of timing out
        if thread is None:
            thread = self.topic_for(project_key, project_name, session)
        if silent is None:
            silent = not self.away
        ids = []
        for i, body in enumerate(text if isinstance(text, list) else [text]):
            last = i == (len(text) - 1 if isinstance(text, list) else 0)
            try:
                msg = self.bot.send_message(
                    self.cfg["chat_id"], body, thread_id=thread,
                    markup=markup if last else None, silent=silent,
                )
                ids.append(msg["message_id"])
            except TelegramError as e:
                log("sendMessage failed:", e)
                if _thread_gone(e) and thread is not None:
                    # The topic was deleted in Telegram. Its id is still in
                    # state, so every message for that project would be posted
                    # into nothing: forget it, make a new one, and deliver.
                    self.forget_topic(thread)
                    thread = self.topic_for(project_key, project_name, session)
                    try:
                        msg = self.bot.send_message(
                            self.cfg["chat_id"], body, thread_id=thread,
                            markup=markup if last else None, silent=silent,
                        )
                        ids.append(msg["message_id"])
                    except TelegramError as e2:
                        log("retry in a fresh topic failed:", e2)
                    continue
                if e.code == 400 and "parse" in (e.description or "").lower():
                    # Never lose content to a formatting error: retry as plain.
                    try:
                        msg = self.bot.send_message(
                            self.cfg["chat_id"], _strip_tags(body), thread_id=thread,
                            markup=markup if last else None, html=False, silent=silent,
                        )
                        ids.append(msg["message_id"])
                    except TelegramError as e2:
                        log("plain retry failed:", e2)
        return ids

    def should_log(self, kind):
        """Away mode reports everything; at the PC only what you asked for."""
        if self.away:
            return True
        return kind in (self.cfg.get("log_when_present") or [])

    # -- live message streaming ------------------------------------------

    def track_transcript(self, session_id, path):
        """Start tailing from the end: history is not replayed into the chat."""
        if not path:
            return
        with self.lock:
            session = self.sessions.setdefault(session_id, {})
            if session.get("tail_path") == path:
                return
            try:
                offset = os.path.getsize(path)
            except OSError:
                offset = 0
            session["tail_path"] = path
            session["tail_offset"] = offset

    def tail_forever(self):
        while True:
            time.sleep(1.0)
            try:
                self.tail_once()
            except Exception as e:
                log("tail error:", type(e).__name__, e)

    def tail_once(self):
        if not self.cfg.get("live_messages", True):
            return
        if not self.should_log("live"):
            # Muted at the PC. The offset is deliberately NOT consumed: switch
            # to away mid-turn and the accrued text still flows; stay at the
            # PC and on_stop settles the tail with its closing message.
            return
        # Serialized against the background ticker: on_stop flushes through
        # here too, and must not observe streamed_this_turn while the ticker
        # has already consumed the tail but is still delivering it — that gap
        # made the closing message repeat text the chat was about to receive.
        with self.tail_lock:
            with self.lock:
                watched = [(sid, dict(s)) for sid, s in self.sessions.items()
                           if s.get("tail_path")]
            for session_id, session in watched:
                for text in self.read_new_messages(session_id, session):
                    self.emit_live(session_id, session, text)

    def read_new_messages(self, session_id, session):
        """Complete assistant text blocks appended since the last read."""
        path, offset = session.get("tail_path"), session.get("tail_offset", 0)
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        if size < offset:          # transcript rotated or replaced
            offset = 0
        if size == offset:
            return []
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
        except OSError:
            return []

        # Stop at the last newline: a trailing partial line is still being
        # written and would not parse.
        cut = chunk.rfind(b"\n")
        if cut == -1:
            return []
        consumed = offset + cut + 1
        with self.lock:
            live = self.sessions.get(session_id)
            if live is not None:
                live["tail_offset"] = consumed

        messages = []
        for line in chunk[:cut].decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") != "assistant" or entry.get("isSidechain"):
                continue
            if entry.get("isApiErrorMessage"):
                continue
            text = transcript.text_of(entry)
            if text:
                messages.append(text)
        return messages

    def emit_live(self, session_id, session, text):
        # Reached only when the stream is allowed (tail_once gates on the
        # "live" kind): away it always flows, at the PC it is opt-in.
        key, name = session.get("project"), session.get("name") or "?"
        if not key:
            return
        ids = self.send(key, name, render.render(text, header=f"💬 <b>{html.escape(name)}</b>"),
                        silent=not self.away, session=session_id)
        if not ids:
            return      # nothing reached the chat: let the closing message carry it
        with self.lock:
            live = self.sessions.get(session_id)
            if live is not None:
                live["streamed_this_turn"] = True

    # -- turn tracking, stats, watchdog ---------------------------------

    def clear_turn(self, session_id):
        with self.lock:
            session = self.sessions.get(session_id)
            if session:
                session["turn_started"] = None
                session["stall_alerted"] = False
                session["streamed_this_turn"] = False

    def bump_stat(self, key, field, seconds=0.0):
        today = time.strftime("%Y-%m-%d")
        with self.lock:
            stats = self.state.setdefault("stats", {})
            day = stats.setdefault(today, {})
            entry = day.setdefault(key, {"turns": 0, "failures": 0, "seconds": 0.0})
            entry[field] = entry.get(field, 0) + 1
            entry["seconds"] = entry.get("seconds", 0.0) + (seconds or 0.0)
            self.prune_stats(stats, today)
        self.persist()

    def prune_stats(self, stats, today):
        """Keep the tallies bounded: state.json is rewritten on every turn and
        only the digest reads them, one day at a time. Caller holds the lock."""
        days = int(self.cfg.get("stats_retention_days", 14) or 0)
        if days <= 0 or len(stats) <= days:
            return
        keep = set(sorted(stats)[-days:]) | {today}
        for day in [d for d in stats if d not in keep]:
            stats.pop(day, None)

    def watchdog_forever(self, minutes):
        limit = max(60, int(minutes) * 60)
        while True:
            time.sleep(30)
            # Flag checked per cycle, not at startup: toggling it in the widget
            # must take effect without restarting the daemon.
            if not (self.cfg.get("watchdog") or {}).get("enabled", True):
                continue
            try:
                self.check_stalls(limit)
                self.forget_stale_sessions()
            except Exception as e:
                log("watchdog error:", type(e).__name__, e)

    def check_stalls(self, limit):
        now = time.time()
        stalled = []
        with self.lock:
            for sid, session in self.sessions.items():
                started = session.get("turn_started")
                if not started or session.get("stall_alerted"):
                    continue
                if now - started >= limit:
                    session["stall_alerted"] = True
                    stalled.append((session.get("project"), session.get("name"),
                                    now - started, sid))
        for key, name, elapsed, sid in stalled:
            # Into the thread of the session that is stuck, not the project's:
            # "this one has not moved in twenty minutes" is only useful next
            # to the conversation it is about.
            self.send(key, name or "?",
                      t("stall", name=html.escape(name or "?"),
                        duration=render.duration(elapsed)), silent=False,
                      session=sid)

    def digest_forever(self, hour):
        while True:
            time.sleep(self.seconds_until(hour))
            if not (self.cfg.get("daily_digest") or {}).get("enabled", True):
                continue
            try:
                self.send_digest(time.strftime("%Y-%m-%d"))
            except Exception as e:
                log("digest error:", type(e).__name__, e)
            time.sleep(60)  # never fire twice within the same minute

    @staticmethod
    def seconds_until(hour):
        now = time.localtime()
        target = time.struct_time((now.tm_year, now.tm_mon, now.tm_mday, int(hour),
                                   0, 0, now.tm_wday, now.tm_yday, now.tm_isdst))
        delta = time.mktime(target) - time.time()
        return delta if delta > 30 else delta + 86400

    def send_digest(self, day):
        with self.lock:
            stats = dict(self.state.get("stats", {}).get(day) or {})
        if not stats:
            return
        lines = [f"**{t('digest.title', day=day)}**", ""]
        rows = [(t("digest.project"), t("digest.turns"),
                 t("digest.errors"), t("digest.time"))]
        for key, entry in sorted(stats.items()):
            meta = self.cfg["projects"].get(key) or {}
            rows.append((meta.get("name") or os.path.basename(key.rstrip("/")),
                         str(entry.get("turns", 0)), str(entry.get("failures", 0)),
                         render.duration(entry.get("seconds", 0))))
        widths = [max(len(r[c]) for r in rows) for c in range(4)]
        table = [" ".join(cell.ljust(widths[c]) for c, cell in enumerate(row)).rstrip()
                 for row in rows]
        table.insert(1, " ".join("-" * w for w in widths))
        lines.append("```\n" + "\n".join(table) + "\n```")
        self.send(self.STATUS_KEY, t("topic.status"),
                  render.render("\n".join(lines)), silent=True)

    def git_summary(self, cwd):
        """Short 'what is left uncommitted' note; silent on non-git projects."""
        if not cwd or not os.path.isdir(os.path.join(cwd, ".git")):
            return ""
        try:
            porcelain = subprocess.run(
                ["git", "status", "--porcelain"], cwd=cwd, capture_output=True,
                text=True, timeout=10, encoding="utf-8", errors="replace")
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd,
                capture_output=True, text=True, timeout=10, encoding="utf-8",
                errors="replace")
        except (OSError, subprocess.SubprocessError):
            return ""
        changes = [l for l in (porcelain.stdout or "").splitlines() if l.strip()]
        head = (branch.stdout or "").strip() or "?"
        if not changes:
            return t("git.clean", branch=html.escape(head))
        shown = "\n".join(changes[:15])
        more = ("\n" + t("git.more", count=len(changes) - 15)
                if len(changes) > 15 else "")
        return (t("git.dirty", branch=html.escape(head), count=len(changes)) +
                f"\n<pre>{html.escape(shown)}{html.escape(more)}</pre>")

    # -- usage / rate limits --------------------------------------------

    USAGE_KEY = "__usage__"

    def report_usage(self):
        """Separate message in a fixed topic, as a running limits log."""
        cfg = self.cfg.get("usage_report") or {}
        if not cfg.get("enabled", True):
            return
        if not self.should_log("usage"):
            return
        data = usage.load(max_age=cfg.get("max_age_seconds", 3600))
        if not data:
            return
        body = usage.report(data)
        if not body:
            return
        self.send(self.USAGE_KEY, t("topic.usage"),
                  render.render(body, header=t("usage.header")), silent=True)

    def usage_poll_forever(self):
        """Keep the rate-limit cache fresh when the status line never runs.

        Claude Code renders the status line only in its terminal UI, so a
        VSCode-only setup writes usage.json exactly never. The poll fills that
        gap and stays out of the way otherwise: a cache the status line just
        wrote is left alone.
        """
        failures = 0
        # The cadence the poll last settled on. Kept in state as well: knowing
        # "every four minutes, because every two earns a refusal" is the whole
        # lesson, and starting over at the configured interval after every
        # restart is how the lesson was unlearned a dozen times a day.
        floor = self.state.get("usage_floor") or 0
        wins = 0
        # A restart used to wipe the server's cooldown and ask again at once,
        # which is how a reboot turned a refusal into a longer one. It is kept
        # in state, so the wait continues where it left off.
        left = self.usage_hold_left()
        if left:
            log(f"usage poll held for {int(left)}s by an earlier refusal")
            time.sleep(left)
        while True:
            cfg = self.cfg.get("usage_poll") or {}
            interval = max(10, int(cfg.get("interval_seconds", 30)))
            delay = max(interval, floor)
            # One sleep, at the bottom, on every path: an early `continue`
            # here turns the whole thread into a busy loop.
            if cfg.get("enabled", False):
                try:
                    if usage.status_line_fresh(
                            int(cfg.get("stale_after_seconds", 45))):
                        failures = 0        # the status line is doing the work
                    else:
                        record = usage.fetch_live(
                            timeout=int(cfg.get("timeout_seconds", 10)))
                        if record:
                            usage.store(record)
                            if failures:
                                log("usage poll recovered")
                            failures = 0
                            wins += 1
                            if floor and wins >= self.USAGE_CALM:
                                floor = self.calm_poll(floor, interval)
                                self.remember_floor(floor)
                                wins = 0
                        else:
                            failures += 1   # no token yet, or nothing to store
                except usage.UsageError as e:
                    failures += 1
                    wins = 0
                    if failures in (1, 5):  # once when it breaks, once when stuck
                        log("usage poll failed:", e)
                    asked = getattr(e, "retry_after", 0)
                    if asked:
                        # Waiting exactly as long as the server says and then
                        # asking again at the old rate earns the same refusal
                        # every other minute. The cadence itself is what the
                        # server is objecting to, so widen that instead.
                        floor = self.widen_poll(floor, interval, asked)
                        delay = max(asked, floor)
                        self.hold_usage(delay, floor)
                        time.sleep(delay)
                        continue
                except Exception as e:
                    failures += 1
                    log("usage poll error:", type(e).__name__, e)
                # Backing off keeps an expired token or a dead network from
                # turning into two requests a minute for the rest of the day.
                delay = max(floor, min(interval * (2 ** min(failures, 5)), 900))
            else:
                failures = 0
            time.sleep(delay)

    def hold_usage(self, seconds, floor=None):
        """Remember how long the server asked to be left alone, and at what
        pace we were asking when it said so."""
        with self.lock:
            self.state["usage_hold_until"] = time.time() + max(0, seconds)
            if floor:
                self.state["usage_floor"] = floor
        self.persist()

    def remember_floor(self, floor):
        with self.lock:
            self.state["usage_floor"] = floor or 0
        self.persist()

    def usage_hold_left(self):
        with self.lock:
            until = self.state.get("usage_hold_until") or 0
        return max(0, until - time.time())

    def poll_usage_now(self):
        """Fetch the limits this second, for the refresh button.

        Honours the server's cooldown rather than spending it: a button that
        earns a longer ban every time it is pressed is worse than no button.
        """
        left = self.usage_hold_left()
        if left:
            return {"ok": False, "wait": int(left), "usage": usage.load()}
        try:
            record = usage.fetch_live(
                timeout=int((self.cfg.get("usage_poll") or {})
                            .get("timeout_seconds", 10)))
        except usage.UsageError as e:
            asked = getattr(e, "retry_after", 0)
            if asked:
                self.hold_usage(asked)
            return {"ok": False, "wait": int(asked),
                    "error": str(e)[:200], "usage": usage.load()}
        if not record:
            return {"ok": False, "error": "no limits in the answer",
                    "usage": usage.load()}
        usage.store(record)
        return {"ok": True, "usage": record}

    # -- self-update -----------------------------------------------------

    def idle_enough_to_update(self):
        """No session is running, waiting on you, or being spawned.

        An update closes the daemon, and a hook blocked on a question would go
        down with it — the session would lose the answer you were about to
        give. Idle is the only safe moment.
        """
        with self.lock:
            live = any(self.session_alive(s) for s in self.sessions.values())
            return not live and not self.waiters and not self.spawning

    def update_forever(self):
        """Check for a release, and install it the first time it is safe to."""
        pending = None
        while True:
            cfg = self.cfg.get("updates") or {}
            hours = max(1, int(cfg.get("interval_hours", 6)))
            if not cfg.get("enabled", True) or not paths.frozen():
                # From source the update is `git pull`; nothing to do here.
                time.sleep(hours * 3600)
                continue
            try:
                if pending is None:
                    pending = updater.available()
                    if pending:
                        log(f"update available: {pending['version']} "
                            f"(installed {version.__version__})")
                        self.announce_update(pending)
                if pending and self.idle_enough_to_update():
                    self.apply_update(pending)
                    pending = None      # the installer takes it from here
            except updater.UpdateError as e:
                log("update check failed:", e)
            except Exception as e:
                log("update error:", type(e).__name__, e)
            # Re-check the pending one every few minutes so it lands as soon
            # as the bridge falls quiet, instead of waiting a whole interval.
            time.sleep(300 if pending else hours * 3600)

    def apply_update(self, release):
        skipped = self.state.get("update_failed")
        if skipped == release["version"]:
            return          # already tried this one and it did not verify
        try:
            path = updater.download(release)
        except updater.UpdateError as e:
            log("update download refused:", e)
            with self.lock:
                self.state["update_failed"] = release["version"]
            self.persist()
            return
        log(f"installing {release['version']} from {path}")
        self.notify_update(t("update.installing", version=release["version"]))
        updater.launch(path)

    def announce_update(self, release):
        self.notify_update(t("update.found", version=release["version"]))

    def notify_update(self, text):
        """Updates are worth a line in the status topic, not a per-project one."""
        try:
            self.send(self.STATUS_KEY, t("topic.status"), text, silent=True)
        except Exception as e:
            log("update notice failed:", type(e).__name__, e)

    def update_state(self):
        return {"version": version.__version__, "frozen": paths.frozen(),
                "enabled": bool((self.cfg.get("updates") or {}).get("enabled", True))}

    # -- status page ----------------------------------------------------

    STATUS_KEY = "__status__"

    def log_status(self, message):
        log(message)

    def status_snapshot(self):
        with self.lock:
            return self.state.get("status_snapshot")

    def store_status_snapshot(self, snapshot):
        with self.lock:
            self.state["status_snapshot"] = snapshot
        self.persist()

    def announce_status(self, message):
        # Outages are the one thing worth a notification even at the PC.
        self.send(self.STATUS_KEY, t("topic.status"),
                  render.render(message, header=t("statuspage.header")),
                  silent=False)

    # -- hook events ----------------------------------------------------

    def session_home(self, data):
        """Where this session was opened, which is what it belongs to.

        A shared directory cannot answer this: a game client is worked on from
        four or five projects, and binding it to one of them files everybody
        else's sessions under that one. Each session has its own transcript
        and its own first entry, so each answers for itself.

        Read once per session and remembered. Falls back to the current
        directory when there is no transcript to ask — a session that has one
        is the normal case, and one that does not is no worse off than before.
        """
        session_id = data.get("session_id")
        with self.lock:
            cached = (self.sessions.get(session_id) or {}).get("home")
        if cached:
            return cached
        home = transcript.home_cwd(data.get("transcript_path"))
        home = home or data.get("cwd") or ""
        if session_id:
            with self.lock:
                self.sessions.setdefault(session_id, {})["home"] = home
        return home

    def resolve_project(self, data):
        """(key, display name) for an event, or None to stay out of the way.

        Order matters: a session already known keeps the project it opened
        in, whatever directory it is standing in now; otherwise an explicit
        `projects` entry wins so custom names and extra_paths keep working,
        and auto-discovery is the fallback.
        """
        cwd = data.get("cwd") or ""
        transcript = data.get("transcript_path")
        if cfgmod.is_excluded(self.cfg, cwd, transcript):
            return None

        session_id = data.get("session_id")
        with self.lock:
            known = self.sessions.get(session_id) or {}
            if known.get("project"):
                # A session belongs to the project it opened in, for as long
                # as it lives. `cwd` is wherever it happens to be standing,
                # and a session that steps into another project's tree used to
                # step into that project's topic with it: seen live, a
                # converter working through a game client alternated between
                # two topics for minutes, one line here and the next there,
                # because the client directory is bound to another project.
                return known["project"], known.get("name") or "?"

        match = cfgmod.project_for(self.cfg, self.session_home(data))
        if match:
            key, meta = match
            return key, meta.get("name") or os.path.basename(key.rstrip("/"))

        if not self.cfg.get("auto_discover", True):
            return None

        key = cfgmod.transcript_project_key(transcript)
        if not key:
            return None
        with self.lock:
            cached = (self.state.setdefault("auto_names", {})).get(key)
        if cached:
            return key, cached

        root = cfgmod.project_root_from_transcript(transcript) or cwd
        name = os.path.basename(str(root).rstrip("\\/")) or t("project.fallback")
        with self.lock:
            self.state["auto_names"][key] = name
        self.persist()
        log(f"auto-discovered project: {name} ({root})")
        return key, name

    def handle_event(self, data):
        event = data.get("hook_event_name")
        resolved = self.resolve_project(data)
        if not resolved:
            return {}  # excluded or unidentifiable: stay completely out of the way
        key, name = resolved

        # Register on ANY event, not just turn boundaries: a session whose turn
        # began before the hooks existed would otherwise stay invisible to
        # /status and to live streaming until it happened to finish.
        session_id = data.get("session_id")
        if session_id and event != "SessionEnd":
            with self.lock:
                session = self.sessions.setdefault(session_id, {})
                session.update({"project": key, "name": name, "alive": True,
                                "last_seen": time.time(), "parked": False,
                                "cwd": data.get("cwd") or session.get("cwd")})
            self.track_transcript(session_id, data.get("transcript_path"))
        handler = {
            "SessionStart": self.on_session_start,
            "SessionEnd": self.on_session_end,
            "Stop": self.on_stop,
            "StopFailure": self.on_stop_failure,
            "UserPromptSubmit": self.on_prompt_submit,
            "PreToolUse": self.on_pre_tool_use,
            "PostToolUse": self.on_post_tool_use,
            "PostToolBatch": self.on_post_tool_batch,
            "PostToolUseFailure": self.on_tool_failure,
            "Notification": self.on_notification,
        }.get(event)
        if not handler:
            return {}
        return handler(data, key, name) or {}

    def on_session_start(self, data, key, name):
        sid = data.get("session_id")
        with self.lock:
            self.sessions[sid] = {
                "cwd": data.get("cwd"), "project": key, "name": name,
                "transcript": data.get("transcript_path"),
                "started": time.time(), "alive": True,
            }
        self.track_transcript(sid, data.get("transcript_path"))
        if self.should_log("session"):
            mode = t("mode.away") if self.away else t("mode.atpc")
            self.send(key, name,
                      t("session.opened", name=html.escape(name), mode=mode),
                      silent=True, session=sid)
        return self.drain_queue(key)

    def on_session_end(self, data, key, name):
        session_id = data.get("session_id")
        with self.lock:
            self.state.get("todo_msgs", {}).pop(session_id, None)
        if self.should_log("session"):
            note = t("session.closed", name=html.escape(name))
            if self.cfg.get("git_summary", True):
                summary = self.git_summary(data.get("cwd"))
                if summary:
                    note += f"\n{summary}"
            self.send(key, name, note, silent=True, session=session_id)
        with self.lock:
            self.sessions.pop(session_id, None)
        return {}

    def on_prompt_submit(self, data, key, name):
        """Marks the start of a turn so the watchdog can notice a stall."""
        with self.lock:
            session = self.sessions.setdefault(data.get("session_id"), {})
            session.update({"project": key, "name": name, "alive": True,
                            "cwd": data.get("cwd"),
                            "transcript": data.get("transcript_path"),
                            "turn_started": time.time(), "stall_alerted": False,
                            "streamed_this_turn": False})
        self.track_transcript(data.get("session_id"), data.get("transcript_path"))
        return {}

    def on_stop_failure(self, data, key, name):
        """The turn died on an API error — usually an outage or a rate limit."""
        self.clear_turn(data.get("session_id"))
        self.bump_stat(key, "failures")
        detail = _describe_failure(data) or t("fail.api.text")
        head = render.header("🛑", t("fail.api.head", name=name))
        self.send(key, name, render.render(f"```\n{detail[:1500]}\n```", header=head),
                  silent=False, session=data.get("session_id"))
        return {}

    def on_notification(self, data, key, name):
        message = data.get("message") or ""
        if message and self.should_log("notification"):
            self.send(key, name, f"🔔 {html.escape(message)}",
                      session=data.get("session_id"))
        return {}

    ICONS = {"completed": "✅", "in_progress": "🔸", "pending": "▫️"}

    def on_post_tool_use(self, data, key, name):
        if data.get("tool_name") != "TodoWrite":
            return {}
        todos = (data.get("tool_input") or {}).get("todos") or []
        if todos and self.should_log("todo"):
            self.upsert_todos(data.get("session_id"), key, name, todos)
        return {}

    def on_post_tool_batch(self, data, key, name):
        """Hand over anything typed while the turn was still running.

        Typing into the editor mid-action reaches the session at its next
        model request; a task typed into Telegram used to wait for the turn
        to end, so a correction arrived after the work it was meant to
        correct. This is the same door: the batch has resolved and the next
        request has not been made yet.
        """
        return self.hand_over(key, name, "PostToolBatch", data.get("session_id"))

    def hand_over(self, key, name, event, session_id=None):
        """Give the project's queue to the turn that is running right now.

        Nothing is said when the queue is empty, which is almost every batch
        of almost every turn — this runs on the session's hot path and must
        cost nothing when there is nothing to say.
        """
        with self.lock:
            if not self.queue.get(key):
                return {}
        items = self.take_queue(key)
        if not items:
            return {}
        self.send(key, name, t("queue.handed", task=html.escape(items[0][:300])),
                  silent=True, session=session_id)
        return {"hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": t("queue.live") + "\n" + _join_tasks(items)}}

    def render_todos(self, todos, name):
        done = sum(1 for item in todos if item.get("status") == "completed")
        lines = [t("todo.head", name=html.escape(name), done=done, total=len(todos))]
        for todo in todos:
            status = todo.get("status", "pending")
            label = html.escape(todo.get("content", ""))
            if status == "completed":
                label = f"<s>{label}</s>"
            elif status == "in_progress":
                label = f"<b>{label}</b>"
            lines.append(f"{self.ICONS.get(status, '▫️')} {label}")
        return "\n".join(lines)[:render.LIMIT]

    def upsert_todos(self, session_id, key, name, todos):
        """Keep one editable checklist per session instead of a message flood."""
        text = self.render_todos(todos, name)
        with self.lock:
            store = self.state.setdefault("todo_msgs", {})
            entry = store.get(session_id)
        if entry:
            try:
                self.bot.edit_message(self.cfg["chat_id"], entry, text)
                return
            except TelegramError as e:
                # "message is not modified" means the list did not change.
                if "not modified" in (e.description or "").lower():
                    return
                log("todo edit failed, posting fresh:", e)
        ids = self.send(key, name, text, silent=True, session=session_id)
        if ids:
            with self.lock:
                self.state.setdefault("todo_msgs", {})[session_id] = ids[-1]
            self.persist()

    def on_tool_failure(self, data, key, name):
        # Counted for the daily digest even when the message itself is off.
        self.bump_stat(key, "failures")
        if not self.cfg.get("report_tool_failures", True):
            return {}
        if not self.should_log("failure"):
            return {}
        tool = data.get("tool_name", "?")
        head = render.header("❌", t("fail.tool.head", name=name, tool=tool))
        body = []
        what = _describe_tool(data.get("tool_input") or {})
        if what:
            body.append(f"```\n{what}\n```")
        detail = _describe_failure(data)
        if detail:
            body.append(f"```\n{detail[:1500]}\n```")
        if not body:
            body.append(t("fail.tool.empty"))
        self.send(key, name, render.render("\n\n".join(body), header=head),
                  session=data.get("session_id"))
        return {}

    def on_stop(self, data, key, name):
        sid = data.get("session_id")
        began = time.time()
        text, seconds = transcript.summarize(data.get("transcript_path"))
        with self.lock:
            self.sessions.setdefault(sid, {}).update(
                {"project": key, "name": name, "alive": True,
                 "transcript": data.get("transcript_path"), "cwd": data.get("cwd")})
        self.track_transcript(sid, data.get("transcript_path"))
        # Flush anything written since the last tick so the closing header
        # never arrives before the text it closes.
        self.tail_once()
        if not self.should_log("live"):
            # The muted tail is settled by this turn's closing message: align
            # the offset so a later away-switch does not replay finished turns.
            with self.lock:
                session = self.sessions.get(sid) or {}
                if session.get("tail_path"):
                    try:
                        session["tail_offset"] = os.path.getsize(session["tail_path"])
                    except OSError:
                        pass
        # Read before clear_turn resets it, or every turn would look unstreamed.
        with self.lock:
            streamed = bool((self.sessions.get(sid) or {}).get("streamed_this_turn"))
        self.clear_turn(sid)
        self.bump_stat(key, "turns", seconds or 0)

        queued = self.take_queue(key)
        if queued:
            # A task was sent while nobody was listening: run it now.
            self.send(key, name, self.closing(text, render.header("✅", name, seconds),
                                              streamed), session=sid)
            self.report_usage()
            self.send(key, name, t("queue.next", task=html.escape(queued[0][:300])),
                      silent=True, session=sid)
            return {"decision": "block", "reason": _join_tasks(queued)}

        head = render.header("✅", name, seconds)
        reported = False
        if not self.away:
            # Say it now rather than after the hold below, or a turn that ends
            # while nobody is at the desk would keep its summary to itself for
            # as long as the hold lasts.
            if self.should_log("stop"):
                self.send(key, name, self.closing(text, head, streamed),
                          silent=True, session=sid)
            self.report_usage()
            reported = True
            self.await_switch()
            if not self.away:
                return {}

        thread = self.topic_for(key, name, sid)
        # The summary has already been sent when the switch arrived late; only
        # the buttons are still owed.
        msgs = ([t("stop.again")] if reported
                else self.closing(text, head, streamed))
        msgs[-1] += f"\n\n<i>{t('stop.await')}</i>"
        while True:
            waiter = Waiter("stop", sid, key)
            markup = {"inline_keyboard": [
                [{"text": t("btn.continue"), "callback_data": f"go:{waiter.id}"},
                 {"text": t("btn.hold"), "callback_data": f"hold:{waiter.id}"}],
                [{"text": t("btn.enough"), "callback_data": f"end:{waiter.id}"}],
            ]}
            ids = self.send(key, name, msgs, markup=markup, thread=thread)
            if not reported:
                self.report_usage()
                reported = True
            self.register(waiter, key, ids, thread)

            # Whatever the hold already spent comes out of the wait: both
            # happen inside one hook, and together they must stay under the
            # timeout Claude Code gives it.
            left = self.cfg["wait_seconds"] - (time.time() - began)
            alarm = self.alarm_seconds()
            if alarm:
                # Check back rather than wait the whole four hours out: the
                # session is only reachable while a turn of it is running, so
                # coming back often is what keeps it reachable at all.
                left = min(left, alarm)
            result = waiter.wait(max(1, left))
            self.unregister(waiter)
            if not result:
                if alarm:
                    self.send(key, name, t("alarm.armed", minutes=alarm // 60),
                              silent=True, session=sid)
                    return {"decision": "block",
                            "reason": t("alarm.set", seconds=alarm)}
                self.park(sid)
                self.send(key, name, t("stop.parked") if self.can_spawn(key)
                          else t("stop.timeout"), silent=True, session=sid)
                return {}
            if result.get("released"):
                # Back at the keyboard, so the hook goes back to the editor —
                # but stepping away again a moment later used to find nothing
                # left to hold. Hold once more while the desk stays quiet.
                self.send(key, name, t("stop.released"), silent=True, session=sid)
                self.await_switch()
                if self.away:
                    msgs = [t("stop.again") + f"\n\n<i>{t('stop.await')}</i>"]
                    continue
                return {}
            if result.get("action") == "hold" and alarm:
                # Waiting is exactly what the alarm is for: without it the
                # session goes quiet here and the round of checking back that
                # was running stops with it.
                self.send(key, name, t("alarm.armed", minutes=alarm // 60),
                          silent=True, session=sid)
                return {"decision": "block", "reason": t("alarm.set", seconds=alarm)}
            if result.get("action") == "hold":
                # Let the turn end without saying a word to the session: it set
                # itself something to wake it, and the wait was the only thing
                # standing between it and its own alarm. A task typed after this
                # is handed over on the session's next batch, not at the next end
                # of turn, so waiting here costs nothing.
                self.send(key, name, t("stop.hold"), silent=True, session=sid)
                return {}
            if result.get("action") == "end":
                return {}
            return {"decision": "block", "reason": result["text"]}

    def closing(self, text, head, streamed=False):
        """Turn ending.

        Repeat the text unless it demonstrably already reached the chat. The
        decision rests on what was actually streamed this turn, not on the
        config flag: a daemon restarted mid-turn streams nothing, and trusting
        the flag there loses the whole turn's text.
        """
        if self.cfg.get("live_messages", True) and streamed:
            return [head]
        return render.render(text, header=head)

    def on_pre_tool_use(self, data, key, name):
        if data.get("tool_name") != "AskUserQuestion":
            return {}
        if not self.away:
            return {}  # at the PC: answer in VSCode as usual
        questions = (data.get("tool_input") or {}).get("questions") or []
        if not questions:
            return {}

        session_id = data.get("session_id")
        thread = self.topic_for(key, name, session_id)
        waiter = Waiter("ask", session_id, key,
                        {"questions": questions, "answers": {}})
        ids = []
        for qi, q in enumerate(questions):
            ids += self.send(key, name, self.render_question(q, qi, len(questions), name),
                             markup=self.question_markup(waiter.id, qi, q),
                             thread=thread)
        self.register(waiter, key, ids, thread)

        result = waiter.wait(self.cfg["wait_seconds"])
        self.unregister(waiter)
        if not result or result.get("released"):
            self.send(key, name, t("ask.timeout"), silent=True, session=session_id)
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": _format_answers(result["answers"]),
            }
        }

    def render_question(self, q, qi, total, name):
        # Built as markdown, not HTML: render() escapes its input, so any tags
        # written here would reach Telegram as visible text.
        head = render.header("❓", t("ask.head", name=name, index=qi + 1, total=total))
        body = [f"**{q.get('question', '')}**"]
        for oi, opt in enumerate(q.get("options", [])):
            line = f"{oi + 1}. **{opt.get('label', '')}**"
            description = (opt.get("description") or "").strip()
            if description:
                line += f"\n   {description}"
            body.append(line)
        if q.get("multiSelect"):
            body.append(t("ask.multi"))
        body.append(t("ask.reply"))
        return render.render("\n\n".join(body), header=head)

    def question_markup(self, wid, qi, q):
        multi = bool(q.get("multiSelect"))
        rows = [[{"text": f"{oi + 1}. {opt.get('label', '')[:40]}",
                  "callback_data": f"{'m' if multi else 'a'}:{wid}:{qi}:{oi}"}]
                for oi, opt in enumerate(q.get("options", []))]
        if multi:
            rows.append([{"text": t("btn.done"), "callback_data": f"d:{wid}:{qi}"}])
        return {"inline_keyboard": rows}

    # -- waiter bookkeeping ---------------------------------------------

    def register(self, waiter, key, message_ids, thread=None):
        waiter.message_ids = message_ids
        waiter.thread = (thread if thread is not None
                         else self.state["topics"].get(key))
        with self.lock:
            self.waiters[waiter.id] = waiter
            self.by_topic[waiter.thread] = waiter.id

    def unregister(self, waiter):
        with self.lock:
            self.waiters.pop(waiter.id, None)
            if self.by_topic.get(waiter.thread) == waiter.id:
                self.by_topic.pop(waiter.thread, None)

    # -- headless spawn ---------------------------------------------------

    # The CLI validates the mode itself and refuses to start on an unknown
    # value, so anything outside this set means "omit the flag entirely".
    SPAWN_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual",
                   "dontAsk", "plan")

    def spawn_cfg(self):
        return self.cfg.get("spawn") or {}

    # A window closed without a SessionEnd leaves a session that looks alive
    # forever: it holds a numbered thread and convinces spawn that someone is
    # listening, so tasks queue up for a session that will never come back.
    SESSION_TTL = 6 * 3600

    def session_alive(self, session):
        if not (session or {}).get("alive"):
            return False
        seen = session.get("last_seen") or session.get("started")
        # No timestamp is not evidence of death — a session registered a
        # moment ago has not been stamped yet, and treating that as stale
        # would hand its thread away while it is still working.
        return seen is None or time.time() - seen < self.SESSION_TTL

    def live_session(self, key):
        """Is a session of this project in a position to take a task?

        A parked one is not. Its window is still open, but its wait ran out
        and control went back to the editor, so it hears nothing from the chat
        until somebody types in that window. Counting it as live is what made
        a task sit in the queue for a session that was never coming back.
        """
        with self.lock:
            return any(self.session_alive(s) and not s.get("parked")
                       and s.get("project") == key
                       for s in self.sessions.values())

    def await_switch(self):
        """Hold a finished turn for a moment, in case the switch is coming.

        A turn that ends at the keyboard let its hook go at once, and with the
        hook went the only way back into that session: flipping to away a
        minute later reached a session nothing fires in any more, and a task
        typed from the phone could do nothing but wait for the next thing
        typed at the desk. Holding keeps that door open while the desk stays
        untouched.

        What ends the hold is a touch that happens *after* the turn did, not a
        recent one: the key press that submitted the prompt of a short turn is
        seconds old when it ends, and treating that as "still here" would shut
        the door in exactly the case this exists for.
        """
        cfg = self.cfg.get("stop_grace") or {}
        if not cfg.get("enabled", True):
            return
        grace = int(cfg.get("seconds") or 0)
        if grace <= 0:
            return
        if input_idle_seconds() is None:
            # No way to notice somebody sitting down again, and holding a turn
            # that only the switch could release would strand every one of
            # them for the full window. Without the signal, do not hold.
            return
        started = time.time()
        while True:
            waited = time.time() - started
            if waited >= grace:
                return
            if self.away:
                log(f"turn held {int(waited)}s and caught the switch")
                return
            idle = input_idle_seconds()
            if idle is not None and idle < waited:
                if foreground_app() in CHAT_APPS:
                    # Reaching for the switch is not coming back to work. The
                    # click that presses it counts as input, and it lands here
                    # a second before the switch does, so counting it let go of
                    # the very turn the press was meant to keep.
                    time.sleep(0.25)
                    continue
                # Any touch may still be the hand on the switch — the widget
                # sits on this screen too, and a press there is a click like
                # any other. Wait out the round trip before deciding it was
                # somebody sitting back down to work.
                settle = time.time() + self.SWITCH_LINGER
                while time.time() < settle:
                    if self.away:
                        log(f"turn held {int(time.time() - started)}s"
                            " and caught the switch")
                        return
                    time.sleep(0.1)
                return          # somebody is at the desk: do not hold them up
            time.sleep(0.25)

    def alarm_seconds(self):
        """How long the session should sleep before checking back, or 0.

        Nothing outside a session can start a turn in it. A wait that runs out
        leaves it out of reach until somebody types at the keyboard — but a
        session that starts something in the background is woken when that
        thing finishes, and a turn it wakes into is a turn the chat can reach.
        """
        cfg = self.cfg.get("wake_alarm") or {}
        if not cfg.get("enabled"):
            return 0
        return max(60, int(cfg.get("minutes") or 15) * 60)

    def working_session(self, key):
        """Is a turn of this project running right now?

        live_session() answers a different question — whether a session could
        take work at all — and one that finished its turn at the keyboard
        answers yes to that while nothing whatsoever is running. Promising a
        handoff there promises a step that is not coming.
        """
        with self.lock:
            return any(self.session_alive(s) and not s.get("parked")
                       and s.get("project") == key and s.get("turn_started")
                       for s in self.sessions.values())

    def idle_sessions(self):
        """Projects whose session finished its turn before the switch flipped.

        Its Stop hook has already answered and gone, so nothing fires in it
        again until somebody types in the editor: it cannot be reached from
        the chat however alive it looks. Seen live — a turn ended at 12:04:59,
        away went on a minute later, and the task typed after that was told a
        working session would take it at its next step. There was no next step.

        Reported, not parked. Parking is the verdict that the window has been
        abandoned, which is what lets an agent be started for the project, and
        a session that stopped typing a minute ago has abandoned nothing — the
        first version of this parked them and a message meant for the session
        in the editor started a second agent in the same directory instead.

        Returns the projects that are out of reach, one entry each.
        """
        idle = {}
        with self.lock:
            waited = {w.session_id for w in self.waiters.values()}
            for sid, session in self.sessions.items():
                if (self.session_alive(session) and not session.get("parked")
                        and not session.get("turn_started") and sid not in waited):
                    idle.setdefault(session.get("project"),
                                    (session.get("name") or "", sid))
        return [(key, name, sid) for key, (name, sid) in idle.items()]

    def park(self, session_id):
        """The session stopped listening to the chat. Reversed by its next
        event, which only happens when it is used at the keyboard again."""
        with self.lock:
            session = self.sessions.get(session_id)
            if session:
                session["parked"] = True

    def can_spawn(self, key):
        """Whether a task typed now would start a session rather than queue."""
        return bool(self.spawn_cfg().get("enabled")) and bool(self.spawn_cwd(key))

    def rescue_answer(self, data, result):
        """Put an answer back in play when its session died before hearing it.

        The reply was typed, acknowledged, and then had nowhere to go. Long
        waits make this reachable: four hours is plenty of time to close the
        editor. Treat it as a task, exactly as if it had been typed a moment
        later — a session picks it up, or an agent is started for it.
        """
        task = (result or {}).get("reason")
        if not task:
            return
        session_id = data.get("session_id")
        if session_id:
            self.park(session_id)   # it answered nothing; it is not listening
        found = cfgmod.project_for(self.cfg, data.get("cwd") or "")
        key = found[0] if found else None
        if not key:
            return
        if self.try_spawn(key, task):
            return
        with self.lock:
            self.queue.setdefault(key, []).append(task)
        self.persist()
        self.send(key, self.project_name(key),
                  t("queue.rescued", task=html.escape(task[:300])), silent=False,
                  session=session_id)

    def forget_stale_sessions(self):
        """Drop sessions nothing has been heard from in hours."""
        with self.lock:
            stale = [sid for sid, s in self.sessions.items()
                     if not self.session_alive(s)]
            for sid in stale:
                self.sessions.pop(sid, None)
        if stale:
            log(f"forgot {len(stale)} session(s) with no events for "
                f"{self.SESSION_TTL // 3600}h")

    def spawn_cwd(self, key):
        """The directory a spawned agent should run in, or None.

        Auto-discovered projects are keyed by their transcript folder under
        ~/.claude/projects, which is not the project tree; those can only be
        queued. A picked project carries its real path as the key.
        """
        meta = (self.cfg.get("projects") or {}).get(key)
        if meta is None:
            return None
        for root in cfgmod.roots_of(key, meta):
            if os.path.isdir(root):
                return root
        return None

    def spawn_command(self, task):
        cfg = self.spawn_cfg()
        binary = cfg.get("command") or shutil.which("claude")
        if not binary:
            return None
        command = [binary, "-p", task]
        if cfg.get("permission_mode") in self.SPAWN_MODES:
            command += ["--permission-mode", cfg["permission_mode"]]
        if cfg.get("model"):
            command += ["--model", str(cfg["model"])]
        return command

    # Everything Claude Code exports to describe the session it is running:
    # ids, entrypoint, effort, permission dirs, and the model pins.
    INHERITED_PREFIXES = ("CLAUDE_",)
    INHERITED_NAMES = ("CLAUDECODE", "ANTHROPIC_MODEL",
                       "ANTHROPIC_DEFAULT_OPUS_MODEL",
                       "ANTHROPIC_DEFAULT_SONNET_MODEL",
                       "ANTHROPIC_DEFAULT_HAIKU_MODEL")

    def spawn_env(self):
        """Environment for a spawned agent, with the parent session stripped.

        The daemon is usually started by a hook, which runs inside a live
        Claude Code session, so its environment describes that session: its id,
        its model pin, its extra working directories. Handing that to a new
        agent makes it a continuation of a session it has nothing to do with —
        seen live, an agent in an empty sandbox listed another project's
        folders as its own. CLAUDECODE is the marker that the environment came
        from a session at all; without it, the user's own variables are left
        exactly as they are.
        """
        env = dict(os.environ)
        if not env.get("CLAUDECODE"):
            return env
        for name in list(env):
            if name.startswith(self.INHERITED_PREFIXES) or name in self.INHERITED_NAMES:
                del env[name]
        return env

    def project_name(self, key):
        meta = (self.cfg.get("projects") or {}).get(key) or {}
        with self.lock:
            auto = (self.state.get("auto_names") or {}).get(key)
        return (meta.get("name") or auto or os.path.basename(key.rstrip("/"))
                or t("project.fallback"))

    def try_spawn(self, key, task, message=None):
        """Give a task a session to land in when none exists.

        The spawned agent is an ordinary Claude Code session, so the installed
        hooks report it back through this very bridge — the live text, the
        closing summary and the git note all arrive with no special handling
        here. Returns True when the task was taken; False leaves it queued.
        """
        if not self.spawn_cfg().get("enabled"):
            return False
        if self.live_session(key):
            return False        # someone is listening: the queue is cheaper
        cwd = self.spawn_cwd(key)
        if not cwd:
            return False
        with self.lock:
            if key in self.spawning:
                return False    # one agent per project; the rest waits in line
            self.spawning.add(key)
        command = self.spawn_command(task)
        if not command:
            with self.lock:
                self.spawning.discard(key)
            log("spawn: no claude executable on PATH")
            self.send(key, self.project_name(key), t("spawn.nocli"), silent=False)
            return False
        threading.Thread(target=self.run_spawn, args=(key, command, cwd, task),
                         daemon=True).start()
        if message:
            self.ack(message, t("ack.spawned"))
        return True

    def run_spawn(self, key, command, cwd, task):
        name = self.project_name(key)
        self.send(key, name, t("spawn.started", name=html.escape(name),
                               task=html.escape(task[:200])), silent=True)
        timeout = int(self.spawn_cfg().get("timeout_seconds") or 0) or None
        log(f"spawn: {name} in {cwd}")
        try:
            result = subprocess.run(
                command, cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                stdin=subprocess.DEVNULL, env=self.spawn_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            self.send(key, name, t("spawn.timeout",
                                   minutes=int((timeout or 0) // 60)), silent=False)
            return
        except (OSError, subprocess.SubprocessError) as e:
            self.send(key, name, t("spawn.failed",
                                   error=html.escape(str(e)[:300])), silent=False)
            return
        finally:
            with self.lock:
                self.spawning.discard(key)
        if result.returncode == 0:
            return              # the hooks already reported the whole run
        detail = (result.stderr or result.stdout or "").strip()[:1000]
        head = render.header("🛑", t("spawn.failed.head", name=name))
        self.send(key, name, render.render(
            f"```\n{detail}\n```" if detail else t("spawn.failed.empty"),
            header=head), silent=False)

    def take_queue(self, key):
        with self.lock:
            items = self.queue.pop(key, [])
        if items:
            self.persist()
        return items

    def drain_queue(self, key):
        items = self.take_queue(key)
        if not items:
            return {}
        return {"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": t("queue.context") + "\n" + _join_tasks(items)}}

    # -- telegram polling ------------------------------------------------

    def poll_forever(self):
        # Nothing in here may raise: a dead poller leaves a daemon that still
        # answers HTTP while being deaf to Telegram, which looks healthy and
        # is not.
        while True:
            if not self.cfg.get("bot_token"):
                # Fresh install: the daemon is up so the widget can reach it
                # and hand it a token. Until then there is nothing to poll.
                time.sleep(2)
                continue
            try:
                self.poll_once()
            except TelegramError as e:
                log("getUpdates error:", e)
                time.sleep(5)
            except Exception as e:
                log("poll loop error:", type(e).__name__, e)
                time.sleep(3)

    def poll_once(self):
        updates = self.bot.get_updates(offset=self.state.get("offset"))
        for update in updates:
            self.state["offset"] = update["update_id"] + 1
            try:
                self.on_update(update)
            except Exception as e:
                log("update handler error:", type(e).__name__, e)
        if updates:
            try:
                self.persist()
            except OSError as e:
                log("state persist failed (continuing):", e)

    def on_update(self, update):
        if "callback_query" in update:
            return self.on_callback(update["callback_query"])
        message = update.get("message")
        if not message:
            return
        text = (message.get("text") or "").strip()
        if not text:
            return
        chat_id = message["chat"]["id"]
        thread = message.get("message_thread_id")
        if text.startswith("/"):
            return self.on_command(text, chat_id, thread, message)
        if self.cfg["chat_id"] and chat_id != self.cfg["chat_id"]:
            return
        if self.is_mode_press(text):
            # The panel button sends its own label as an ordinary message, so
            # it has to be recognised here or it would become a task.
            self.toggle_away()
            try:
                self.bot.delete_message(chat_id, message["message_id"])
            except TelegramError as e:
                log("could not clear the panel press:", e)
            return
        self.deliver_text(text, thread, message)

    @staticmethod
    def is_mode_press(text):
        return text.strip() == t("panel.mode") or text.strip().startswith("🔁")

    def mode_panel(self):
        """One button beside the message box, in every topic of the group.

        A pinned message cannot be made to stick to a topic — pinning is
        chat-wide — and commands are typed by nobody. A reply keyboard sits
        where the typing happens and stays there, so the one thing worth
        reaching for is always within reach.
        """
        return {"keyboard": [[{"text": t("panel.mode")}]],
                "is_persistent": True, "resize_keyboard": True}

    def offer_mode_panel(self):
        """Set the panel once. It survives on its own afterwards."""
        if not self.linked() or self.state.get("panel_sent"):
            return
        try:
            self.bot.send_message(self.cfg["chat_id"], t("panel.ready"),
                                  markup=self.mode_panel(), silent=True)
        except TelegramError as e:
            log("mode panel failed:", e)
            return
        with self.lock:
            self.state["panel_sent"] = True
        self.persist()

    def deliver_text(self, text, thread, message):
        """Route a plain message: to a waiting hook, or onto the project queue."""
        key = self.project_by_topic(thread)
        with self.lock:
            wid = self.by_topic.get(thread)
            waiter = self.waiters.get(wid) if wid else None
            if waiter is None and key:
                # Typed into one of the project's older numbered topics: the
                # session listening on the project is still the right one to
                # hand this to, rather than queueing it in front of itself.
                waiter = next((w for w in self.waiters.values()
                               if w.project == key), None)
        if waiter and waiter.kind == "stop":
            waiter.resolve({"action": "continue", "text": text})
            self.ack(message, t("ack.continue"))
            return
        if waiter and waiter.kind == "ask":
            self.answer_by_text(waiter, text, message)
            return
        if not key or key in {k for k, _ in self.SERVICE_TOPICS}:
            return      # the limits and status topics own no session to task
        if self.try_spawn(key, text, message):
            return
        with self.lock:
            self.queue.setdefault(key, []).append(text)
        self.persist()
        # Three different waits, and calling them all a queue told you
        # nothing about which one you were in. A running turn takes this at
        # its next batch; a session sitting between turns takes it the moment
        # it moves, which may be when you type at the keyboard; with nothing
        # open at all it waits for a session to exist.
        if self.working_session(key):
            said = t("ack.handoff")
        elif self.live_session(key):
            said = t("ack.midstep")
        else:
            said = t("ack.queued")
        self.ack(message, said)

    def answer_by_text(self, waiter, text, message):
        """Free-text reply to a question message answers that question."""
        questions = waiter.payload["questions"]
        qi = 0
        replied = (message.get("reply_to_message") or {}).get("message_id")
        if replied and replied in waiter.message_ids:
            qi = min(waiter.message_ids.index(replied), len(questions) - 1)
        waiter.payload["answers"][questions[qi]["question"]] = text
        self.maybe_finish_ask(waiter)
        self.ack(message, t("ack.answer"))

    def maybe_finish_ask(self, waiter):
        answers = waiter.payload["answers"]
        if len(answers) >= len(waiter.payload["questions"]):
            waiter.resolve({"answers": answers})

    def on_callback(self, cq):
        data = cq.get("data") or ""
        parts = data.split(":")
        if parts[0] == "mode":
            self.toggle_away()
            self.bot.answer_callback(cq["id"], t("cb.mode"))
            return
        if parts[0] == "p":
            if parts[1] == "none":
                return self.bot.answer_callback(cq["id"], t("cb.empty"))
            return self.toggle_project(int(parts[1]), cq)
        waiter = self.waiters.get(parts[1] if len(parts) > 1 else "")
        if not waiter:
            self.bot.answer_callback(cq["id"], t("cb.stale"))
            return
        if parts[0] == "go":
            waiter.resolve({"action": "continue",
                            "text": t("task.default")})
        elif parts[0] == "hold":
            waiter.resolve({"action": "hold"})
        elif parts[0] == "end":
            waiter.resolve({"action": "end"})
        elif parts[0] in ("a", "m", "d"):
            self.on_answer_callback(parts, waiter, cq)
            return
        self.bot.answer_callback(cq["id"], t("cb.accepted"))

    def on_answer_callback(self, parts, waiter, cq):
        kind, _, qi, *rest = parts
        qi = int(qi)
        question = waiter.payload["questions"][qi]
        answers = waiter.payload["answers"]
        label = question["question"]
        if kind == "a":
            answers[label] = question["options"][int(rest[0])]["label"]
            self.bot.answer_callback(cq["id"], t("cb.chosen"))
        elif kind == "m":
            chosen = answers.setdefault(label, [])
            option = question["options"][int(rest[0])]["label"]
            chosen.remove(option) if option in chosen else chosen.append(option)
            self.bot.answer_callback(cq["id"], ", ".join(chosen) or t("cb.cleared"))
            return  # multi-select stays open until "Done"
        elif kind == "d":
            answers.setdefault(label, [])
            self.bot.answer_callback(cq["id"], t("cb.done"))
        self.maybe_finish_ask(waiter)

    # -- commands --------------------------------------------------------

    def on_command(self, text, chat_id, thread, message):
        cmd = text.split()[0].split("@")[0]
        if cmd == "/register":
            return self.on_register(chat_id, thread)
        if self.cfg["chat_id"] and chat_id != self.cfg["chat_id"]:
            return  # commands are only honoured in the registered group
        if cmd in ("/help", "/start"):
            self.bot.send_message(chat_id, t("help.text"), thread_id=thread)
        elif cmd == "/unregister":
            self.on_unregister(chat_id, thread)
        elif cmd in ("/away", "/here"):
            self.set_away(cmd == "/away")
        elif cmd == "/status":
            self.bot.send_message(chat_id, self.status_text(), thread_id=thread,
                                  markup=self.mode_markup())
        elif cmd == "/projects":
            self.show_projects(chat_id, thread)
        elif cmd == "/release":
            freed = self.release_all()
            self.bot.send_message(chat_id, t("released.count", count=freed),
                                  thread_id=thread)
        elif cmd == "/claude_status":
            threading.Thread(target=self.report_status_now,
                             args=(chat_id, thread), daemon=True).start()
        elif cmd == "/queue":
            with self.lock:
                total = {k: len(v) for k, v in self.queue.items() if v}
            self.bot.send_message(chat_id, t("queue.show", items=total or t("queue.empty")),
                                  thread_id=thread)

    def on_register(self, chat_id, thread):
        """Binding is deliberately one-way.

        A bot's username is discoverable, its token is not needed to talk to
        it, and an unguarded /register from a stranger's chat would silently
        redirect every session's contents there. Rebinding requires
        /unregister from the chat that currently owns the bridge.
        """
        bound = self.cfg.get("chat_id")
        if bound and bound != chat_id:
            log(f"/register refused: bound to {bound}, requested from {chat_id}")
            return  # no reply: an outsider learns nothing about this bridge
        if bound == chat_id:
            self.bot.send_message(chat_id, t("register.already"), thread_id=thread)
            return
        self.cfg["chat_id"] = chat_id
        cfgmod.save(self.cfg)
        self.bot.send_message(chat_id, t("register.ok"), thread_id=thread)
        self.ensure_service_topics()
        self.refresh_status()

    def on_unregister(self, chat_id, thread):
        """Release the binding, and forget the topic ids with it: they belong
        to this group and mean nothing in the next one."""
        self.bot.send_message(chat_id, t("register.cleared"), thread_id=thread)
        self.cfg["chat_id"] = None
        cfgmod.save(self.cfg)
        with self.lock:
            self.state["topics"] = {}
            self.state["status_message_id"] = None
        self.persist()
        log("unregistered: group binding and topic ids cleared")

    def show_projects(self, chat_id, thread, message_id=None):
        """Picker: one tappable row per known project, ✅ = bridged."""
        catalogue = self.project_catalogue()[:24]  # Telegram caps keyboard size
        with self.lock:
            self.state["picker"] = [p["key"] for p in catalogue]
        rows = []
        for index, project in enumerate(catalogue):
            mark = "✅" if project["enabled"] else "▫️"
            missing = "" if project["exists"] else f" {t('projects.missing')}"
            rows.append([{"text": f"{mark} {project['name']}{missing}"[:60],
                          "callback_data": f"p:{index}"}])
        markup = {"inline_keyboard": rows or [[{"text": t("projects.none"),
                                                "callback_data": "p:none"}]]}
        text = t("projects.text")
        try:
            if message_id:
                self.bot.edit_message(chat_id, message_id, text, markup=markup)
            else:
                self.bot.send_message(chat_id, text, thread_id=thread, markup=markup)
        except TelegramError as e:
            if "not modified" not in (e.description or "").lower():
                log("projects picker failed:", e)

    def toggle_project(self, index, cq):
        with self.lock:
            picker = list(self.state.get("picker") or [])
        if index >= len(picker):
            return self.bot.answer_callback(cq["id"], t("projects.stale"))
        key = picker[index]
        current = {cfgmod.normalize(k) for k in (self.cfg.get("projects") or {})}
        current.symmetric_difference_update({cfgmod.normalize(key)})
        self.select_projects(current)
        message = cq.get("message") or {}
        self.bot.answer_callback(cq["id"], t("cb.done"))
        self.show_projects(message.get("chat", {}).get("id"),
                           message.get("message_thread_id"),
                           message.get("message_id"))

    def report_status_now(self, chat_id, thread):
        """On-demand answer to /claude_status, fetched off the poller thread."""
        try:
            snap = status.snapshot(status.fetch())
        except Exception as e:
            self.bot.send_message(chat_id, t("statuspage.error", error=e),
                                  thread_id=thread)
            return
        icon = status.INDICATOR_ICON.get(snap["indicator"], "⚪")
        lines = [f"{icon} <b>{html.escape(snap['description'] or '?')}</b>"]
        for comp, state in snap["components"].items():
            lines.append(f"{status.COMPONENT_ICON.get(state, '⚪')} "
                         f"{html.escape(comp)} — {status.state_name(state)}")
        if snap["incidents"]:
            lines.append("")
            for data in snap["incidents"].values():
                lines.append(f"• <b>{html.escape(data['name'] or '')}</b> "
                             f"({data.get('status')})")
        self.bot.send_message(chat_id, "\n".join(lines), thread_id=thread)

    def status_text(self):
        with self.lock:
            live = [s.get("name", "?") for s in self.sessions.values()
                    if self.session_alive(s)]
            pending = len(self.waiters)
        mode = t("mode.status.away") if self.away else t("mode.status.atpc")
        return (f"{mode}\n"
                f"{t('status.sessions', names=', '.join(live) or t('status.none'))}\n"
                f"{t('status.waiting', count=pending)}")

    # -- settings exposed to the widget ----------------------------------

    TOGGLES = [
        "live_messages",
        "report_tool_failures",
        "usage_report.enabled",
        "usage_poll.enabled",
        "status_monitor.enabled",
        "watchdog.enabled",
        "stop_grace.enabled",
        "session_topics.enabled",
        "wake_alarm.enabled",
        "daily_digest.enabled",
        "git_summary",
        "log_when_present.stop",
        "log_when_present.todo",
        "log_when_present.session",
        "log_when_present.live",
        "log_when_present.usage",
        "log_when_present.notification",
        "auto_discover",
        "updates.enabled",
        "spawn.enabled",
        "debug_hooks",
    ]

    def get_setting(self, path):
        head, _, tail = path.partition(".")
        if head == "log_when_present":
            return tail in (self.cfg.get(head) or [])
        value = self.cfg.get(head)
        if tail:
            # A config that spells out a section without the key must read as
            # the shipped default, not as "on" — spawn.enabled defaults off.
            fallback = (cfgmod.DEFAULTS.get(head) or {}).get(tail, True)
            return bool((value or {}).get(tail, fallback))
        return bool(value)

    def set_setting(self, path, enabled):
        head, _, tail = path.partition(".")
        if head == "log_when_present":
            kinds = set(self.cfg.get(head) or [])
            kinds.add(tail) if enabled else kinds.discard(tail)
            self.cfg[head] = sorted(kinds)
        elif tail:
            section = dict(self.cfg.get(head) or {})
            section[tail] = bool(enabled)
            self.cfg[head] = section
        else:
            self.cfg[head] = bool(enabled)

    def settings_snapshot(self):
        return [{"path": path, "label": t("toggle." + path),
                 "enabled": self.get_setting(path)}
                for path in self.TOGGLES]

    def apply_settings(self, patch):
        with self.lock:
            for path, enabled in (patch or {}).items():
                if path == "language":
                    # Not a toggle: a language code, or None for auto-detect.
                    lang = enabled if enabled in i18n.LANGUAGES else None
                    self.cfg["language"] = lang
                    i18n.set_language(lang)
                elif path == "wake_alarm.minutes":
                    # A number, not a switch: zero is how the widget's little
                    # box says "no alarm", so it turns the thing off too.
                    minutes = max(0, min(240, int(enabled or 0)))
                    section = dict(self.cfg.get("wake_alarm") or {})
                    section["enabled"] = minutes > 0
                    section["minutes"] = minutes or section.get("minutes", 15)
                    self.cfg["wake_alarm"] = section
                elif path in self.TOGGLES:
                    self.set_setting(path, enabled)
        cfgmod.save(self.cfg)
        log("settings updated:", ", ".join(f"{k}={v}" for k, v in (patch or {}).items()))
        return self.settings_snapshot()

    # -- Telegram setup, driven from the widget --------------------------

    def telegram_state(self):
        """What the setup screen needs to show. Never the token itself: the
        widget only has to know whether one is there, not what it is."""
        token = self.cfg.get("bot_token") or ""
        state = {"has_token": bool(token),
                 "token_hint": f"…{token[-4:]}" if len(token) > 4 else "",
                 "chat_id": self.cfg.get("chat_id"),
                 "username": self.state.get("bot_username") or ""}
        with self.lock:
            state["topics"] = len([k for k in self.state.get("topics", {})
                                   if not k.startswith("__")])
        return state

    def set_token(self, token):
        """Adopt a token after Telegram itself confirms it works.

        Rejecting a bad token here rather than at startup is the whole point
        of the setup screen: the user pastes, sees the bot's name, and knows
        it took — instead of a daemon that quietly refuses to start.
        """
        token = (token or "").strip()
        if not token:
            return {"ok": False, "error": t("setup.err.empty")}
        try:
            me = Bot(token).get_me()
        except TelegramError as e:
            log("token rejected:", e)
            return {"ok": False, "error": t("setup.err.rejected")}
        except Exception as e:                       # no network, DNS, proxy
            log("token check failed:", type(e).__name__, e)
            return {"ok": False, "error": t("setup.err.offline")}

        changed = token != self.cfg.get("bot_token")
        self.cfg["bot_token"] = token
        if changed:
            # A different bot means a different chat and different topic ids;
            # keeping them would post into threads that are not ours.
            self.cfg["chat_id"] = None
            with self.lock:
                self.state["topics"] = {}
                self.state["status_message_id"] = None
                self.state["offset"] = None
        with self.lock:
            self.state["bot_username"] = me.get("username") or ""
        self.bot = Bot(token)
        cfgmod.save(self.cfg)
        self.persist()
        log(f"token accepted: @{me.get('username')}"
            + (" (binding reset)" if changed else ""))
        return {"ok": True, "username": me.get("username") or "",
                "chat_id": self.cfg.get("chat_id")}

    def hooks_state(self):
        state = hooks.status()
        state["ready"] = state["hooks"] >= state["expected"]
        return state

    def install_hooks(self, enable=True):
        """The one step that used to need a terminal."""
        try:
            timeout = int(self.cfg.get("hook_timeout_seconds",
                                       cfgmod.DEFAULTS["hook_timeout_seconds"]))
            result = (hooks.install(timeout) if enable else hooks.uninstall())
            log("hooks installed" if enable else "hooks removed",
                f"(backup: {result.get('backup')})" if result.get("backup") else "")
        except OSError as e:
            log("hook install failed:", e)
            return {"ok": False, "error": str(e)[:200]}
        state = self.hooks_state()
        state["ok"] = True
        return state

    def project_catalogue(self):
        return discover.list_projects(cfg=self.cfg)

    def select_projects(self, keys):
        """Apply a chosen set and persist it, so the picker is the source
        of truth for what the bridge is allowed to report on."""
        with self.lock:
            discover.apply_selection(self.cfg, keys)
            self.cfg["projects"] = {cfgmod.normalize(k): v
                                    for k, v in self.cfg["projects"].items()}
        cfgmod.save(self.cfg)
        log(f"projects selected: {[v['name'] for v in self.cfg['projects'].values()]}")
        return self.project_catalogue()

    SERVICE_TOPICS = (
        ("__status__", "topic.status"),
        ("__usage__", "topic.usage"),
    )

    def ensure_service_topics(self):
        """Create the service topics up front instead of on first use, so the
        group is readable before anything happens to report."""
        for key, title_key in self.SERVICE_TOPICS:
            title = t(title_key)
            with self.lock:
                existing = self.state["topics"].get(key)
            if existing:
                continue
            try:
                self.topic_for(key, title)
                log(f"service topic created: {title}")
            except TelegramError as e:
                log(f"service topic '{title}' failed:", e)

    def mode_snapshot(self):
        """Compact state for the desktop widget."""
        with self.lock:
            sessions = [s.get("name", "?") for s in self.sessions.values()
                        if self.session_alive(s)]
            waiting = [{"project": w.project, "kind": w.kind}
                       for w in self.waiters.values()]
            queued = sum(len(v) for v in self.queue.values())
        cfg = self.cfg.get("wake_alarm") or {}
        return {"away": self.away, "sessions": sessions,
                "waiting": waiting, "queued": queued,
                "alarm": {"enabled": bool(cfg.get("enabled")),
                          "minutes": int(cfg.get("minutes") or 15)}}

    def toggle_away(self):
        self.set_away(not self.away)

    def set_away(self, value):
        was_away = self.away
        with self.lock:
            self.state["away"] = bool(value)
        self.persist()
        if was_away and not value:
            # Back at the keyboard: never leave a hook blocking on a chat you
            # are no longer reading. Control returns to VSCode immediately.
            self.release_all()
        if value and not was_away:
            # Handing control over does not reach back into a turn that has
            # already ended. Say which sessions those are, at the one moment
            # the answer is useful.
            for key, name, sid in self.idle_sessions():
                if key:
                    self.send(key, name, t("away.idle"), silent=True, session=sid)
        self.refresh_status()

    def release_all(self):
        """Unblock every waiting hook and hand control back to the editor."""
        with self.lock:
            waiters = list(self.waiters.values())
        for waiter in waiters:
            waiter.resolve({"released": True})
        return len(waiters)

    def mode_markup(self):
        """The one button that hands control over and takes it back.

        It lives on the pinned status message, which sits in the group itself
        rather than in any topic — easy to lose behind a dozen threads. /status
        answers with it too, so it is reachable from wherever you are typing.
        """
        return {"inline_keyboard": [[{
            "text": t("pin.here") if self.away else t("pin.away"),
            "callback_data": "mode",
        }]]}

    # Between full passes over every topic, whatever asks for them.
    SWITCH_MIN_GAP = 15
    # How long a touch waits to turn out to have been the switch being
    # pressed. Paid at the end of every turn worked through at the desk, so
    # it buys only the round trip of a click on this screen and no more.
    SWITCH_LINGER = 2

    def forget_topic(self, tid):
        """Drop a topic that no longer exists, so a new one is made instead.

        A deleted topic leaves its id behind in state, and everything aimed at
        it disappears: with a topic per chat this is not hypothetical, since
        the numbered threads of an earlier version are exactly the ones people
        clear out of the group.
        """
        with self.lock:
            for key in [k for k, v in self.state["topics"].items() if v == tid]:
                del self.state["topics"][key]
            (self.state.get("status_msgs") or {}).pop(str(tid), None)
        self.persist()
        log("topic", tid, "is gone; it will be created again on demand")

    def switch_text(self):
        """What the copy in a topic says: the mode and nothing else.

        The full status names the live sessions, so it changes several times a
        minute — and a line that never settles cannot be checked against what
        is on screen without rewriting it every time.
        """
        return t("mode.status.away") if self.away else t("mode.status.atpc")

    def switch_in_topic(self, tid, text, markup):
        """Put the switch at the top of one topic, or bring it up to date.

        Posted once and only ever edited afterwards. The first version fell
        back to posting whenever an edit failed, which on a rate limit meant a
        second copy in every topic, and a pin with it — a refresh turned into
        a storm, and Telegram raised itself over the editor for each one.
        """
        with self.lock:
            mid = (self.state.get("status_msgs") or {}).get(str(tid))
            if mid and (self.state.get("switch_said") or {}).get(str(tid)) == text:
                return          # already says this; nothing to ask Telegram
        if mid:
            try:
                self.bot.edit_message(self.cfg["chat_id"], mid, text, markup=markup)
                return self.remember_switch(tid, text)
            except TelegramError as e:
                if "not modified" in (e.description or "").lower():
                    return self.remember_switch(tid, text)
                if _thread_gone(e):
                    return self.forget_topic(tid)
                if not _message_gone(e):
                    log("topic switch edit failed:", e)
                    return      # transient: the copy that is there still works
        try:
            msg = self.bot.send_message(self.cfg["chat_id"], text, thread_id=tid,
                                        markup=markup, silent=True)
        except TelegramError as e:
            if _thread_gone(e):
                return self.forget_topic(tid)
            log("topic switch failed:", e)
            return
        with self.lock:
            self.state.setdefault("status_msgs", {})[str(tid)] = msg["message_id"]
        self.remember_switch(tid, text)

    def remember_switch(self, tid, text):
        """Only what Telegram actually accepted, so a refusal is retried."""
        with self.lock:
            self.state.setdefault("switch_said", {})[str(tid)] = text

    def switches_forever(self):
        """Keep the buttons honest.

        They were written once, when the mode changed, and a refusal there —
        a rate limit, a hiccup — left a button that said the opposite of the
        truth with nothing to correct it. Saying nothing costs nothing now,
        so this can simply keep checking.
        """
        while True:
            time.sleep(30)
            try:
                self.refresh_topic_switches()
            except Exception as e:
                log("switch refresh error:", type(e).__name__, e)

    def refresh_topic_switches(self, only=None):
        """The switch, pinned in every project topic rather than only the one.

        The status message lives in the group itself, which a forum shows as a
        tab of its own: the one button that hands control over was a tab away
        from every topic the work actually happens in. Each topic keeps its own
        copy, edited in place, so this costs a handful of edits when the mode
        changes and nothing at all in between.
        """
        if not self.linked():
            return
        now = time.time()
        if only is None:
            # One pass touches every topic, so a burst of mode changes would
            # multiply into a burst per topic.
            if now - getattr(self, "_switches_at", 0) < self.SWITCH_MIN_GAP:
                return
            self._switches_at = now
        service = {k for k, _ in self.SERVICE_TOPICS}
        with self.lock:
            topics = [tid for key, tid in self.state["topics"].items()
                      if tid and key not in service]
        text, markup = self.switch_text(), self.mode_markup()
        for tid in topics:
            if only is None or tid == only:
                self.switch_in_topic(tid, text, markup)
        self.persist()

    def refresh_status(self):
        if not self.linked():
            return
        self.refresh_topic_switches()
        markup = self.mode_markup()
        text = self.status_text()
        mid = self.state.get("status_message_id")
        try:
            if mid:
                self.bot.edit_message(self.cfg["chat_id"], mid, text, markup=markup)
                return
        except TelegramError as e:
            if "not modified" in (e.description or "").lower():
                return  # nothing changed; not an error
            log("status edit failed:", e)
        try:
            msg = self.bot.send_message(self.cfg["chat_id"], text, markup=markup, silent=True)
            self.state["status_message_id"] = msg["message_id"]
            self.bot.pin_message(self.cfg["chat_id"], msg["message_id"])
            self.persist()
        except TelegramError as e:
            log("status send failed:", e)

    def project_by_topic(self, thread):
        """The project a thread belongs to.

        The `#2`, `#3` suffixes are leftovers from the numbered threads that
        were tried and removed. Nothing writes to those topics any more, but
        they still exist in groups that ran that version, so a message typed
        into one still finds its project instead of falling on the floor.
        """
        with self.lock:
            for key, tid in self.state["topics"].items():
                if tid == thread:
                    base, sep, slot = key.rpartition("#")
                    return base if sep and slot.isdigit() else key
        return None

    def ack(self, message, text):
        try:
            self.bot.send_message(
                message["chat"]["id"], f"<i>{html.escape(text)}</i>",
                thread_id=message.get("message_thread_id"), silent=True)
        except TelegramError as e:
            log("ack failed:", e)


def _describe_tool(tool_input):
    """The one field that says what was actually attempted."""
    for field in ("command", "file_path", "pattern", "url", "prompt"):
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:600]
    return ""


def _describe_failure(data):
    """Dig the human-readable error out of whatever shape the hook provides."""
    for field in ("tool_response", "tool_result", "error", "message"):
        payload = data.get(field)
        if isinstance(payload, str) and payload.strip():
            return payload.strip()
        if isinstance(payload, dict):
            for inner in ("error", "stderr", "message", "content", "stdout"):
                value = payload.get(inner)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            if payload:
                return json.dumps(payload, ensure_ascii=False)
    return ""


def _strip_tags(text):
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _message_gone(error):
    """The message we meant to edit is not there to edit any more."""
    text = (getattr(error, "description", "") or "").lower()
    return getattr(error, "code", None) == 400 and (
        "message to edit not found" in text or "message can't be edited" in text
        or "message identifier is not specified" in text)


def _thread_gone(error):
    """Telegram's way of saying the topic this was aimed at is not there."""
    return (getattr(error, "code", None) == 400
            and "thread not found" in (error.description or "").lower())


def _join_tasks(items):
    return "\n".join(f"- {t}" for t in items)


def _format_answers(answers):
    lines = [t("answered.header")]
    for question, value in answers.items():
        picked = ", ".join(value) if isinstance(value, list) else value
        lines.append(f"- {question} -> {picked or t('answered.none')}")
    lines.append(t("answered.footer"))
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    daemon_ref = None

    def _reply(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return
        if self.path.startswith("/mode"):
            self.daemon_ref.set_away(bool(data.get("away")))
            return self._reply(self.daemon_ref.mode_snapshot())
        if self.path.startswith("/projects"):
            return self._reply(
                {"projects": self.daemon_ref.select_projects(data.get("keys") or [])})
        if self.path.startswith("/settings"):
            return self._reply(
                {"settings": self.daemon_ref.apply_settings(data.get("settings") or {})})
        if self.path.startswith("/telegram"):
            result = self.daemon_ref.set_token(data.get("token"))
            result.update(self.daemon_ref.telegram_state())
            return self._reply(result)
        if self.path.startswith("/usage"):
            return self._reply(self.daemon_ref.poll_usage_now())
        if self.path.startswith("/hooks"):
            return self._reply(
                self.daemon_ref.install_hooks(bool(data.get("enable", True))))
        try:
            result = self.daemon_ref.handle_event(data)
        except Exception as e:
            log("handle_event error:", type(e).__name__, e)
            result = {}
        try:
            self._reply(result)
        except OSError as e:
            # The hook is gone — the editor was closed, or Claude Code killed
            # it while it waited. Whatever was typed in the chat was accepted
            # and would now vanish with the connection, so hand it back.
            log("hook connection lost:", e)
            self.daemon_ref.rescue_answer(data, result)

    def do_GET(self):
        if self.path.startswith("/mode"):
            return self._reply(self.daemon_ref.mode_snapshot())
        if self.path.startswith("/projects"):
            return self._reply({"projects": self.daemon_ref.project_catalogue()})
        if self.path.startswith("/settings"):
            return self._reply({"settings": self.daemon_ref.settings_snapshot()})
        if self.path.startswith("/telegram"):
            return self._reply(self.daemon_ref.telegram_state())
        if self.path.startswith("/hooks"):
            return self._reply(self.daemon_ref.hooks_state())
        if self.path.startswith("/version"):
            return self._reply(self.daemon_ref.update_state())
        self._reply({"ok": True})

    def log_message(self, *_):
        pass  # the daemon has its own log


class SingleInstanceServer(ThreadingHTTPServer):
    # Windows honours SO_REUSEADDR by letting a second process bind a live
    # port. Disabling it turns the bind into the single-instance lock: two
    # daemons on one token would fight over getUpdates and lose answers.
    allow_reuse_address = False
    daemon_threads = True


def main():
    cfg = cfgmod.load()
    i18n.set_language(cfg.get("language"))
    # Two claims, because they catch different mistakes: the mutex stops a
    # second copy of this build (installed alongside one run from source), the
    # port stops any daemon at all from stealing the same getUpdates stream.
    claim = paths.single_instance("ClaudeTelegramDaemon")
    if claim is None:
        # Written down, not just exited: started from a shortcut there is no
        # console to print to, and silence looks identical to a crash.
        log("another daemon is already running — this one is not needed")
        raise SystemExit(0)
    try:
        server = SingleInstanceServer((cfg["host"], cfg["port"]), Handler)
    except OSError:
        raise SystemExit(
            t("err.port", port=cfg["port"]))

    daemon = Daemon(cfg)
    Handler.daemon_ref = daemon
    if cfg.get("bot_token"):
        try:
            me = daemon.bot.get_me()
            daemon.state["bot_username"] = me.get("username") or ""
            log(f"bot @{me.get('username')} ready; away={daemon.away}")
        except (TelegramError, OSError) as e:
            # Never fatal: the widget's setup screen is how a bad or expired
            # token gets replaced, and it can only reach a daemon that runs.
            log("token not usable yet:", e)
        if cfg["chat_id"]:
            daemon.ensure_service_topics()
            daemon.offer_mode_panel()
            daemon.refresh_status()
    else:
        log("no bot token yet — waiting for the setup screen")

    threading.Thread(target=daemon.poll_forever, daemon=True).start()

    threading.Thread(target=daemon.tail_forever, daemon=True).start()

    # Threads always run; each honours its own flag per cycle so the widget
    # can switch features on and off without a restart.
    monitor_cfg = cfg.get("status_monitor") or {}
    monitor = status.Monitor(daemon, monitor_cfg.get("interval_seconds", 300),
                             monitor_cfg.get("components"))
    threading.Thread(target=monitor.run_forever, daemon=True).start()
    threading.Thread(target=daemon.watchdog_forever,
                     args=((cfg.get("watchdog") or {}).get("minutes", 20),),
                     daemon=True).start()
    threading.Thread(target=daemon.digest_forever,
                     args=((cfg.get("daily_digest") or {}).get("hour", 21),),
                     daemon=True).start()
    threading.Thread(target=daemon.usage_poll_forever, daemon=True).start()
    threading.Thread(target=daemon.update_forever, daemon=True).start()
    threading.Thread(target=daemon.switches_forever, daemon=True).start()
    log("background workers started (status, watchdog, digest, usage, updates)")

    log(f"listening on http://{cfg['host']}:{cfg['port']}")
    server.serve_forever()


if __name__ == "__main__":
    main()
