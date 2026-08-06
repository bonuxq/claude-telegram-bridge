"""End-to-end exercise of the daemon's event handling with a fake Telegram."""

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claudetg import config as cfgmod  # noqa: E402
from claudetg import daemon as daemon_module  # noqa: E402
from claudetg.daemon import Daemon  # noqa: E402
from claudetg import i18n, paths, render, usage  # noqa: E402

# The assertions below are written against the Russian bundle; pin it so the
# suite does not depend on the OS language of whoever runs it.
i18n.set_language("ru")

# Keep test noise out of the production log, or a real diagnosis later will
# stumble over lines written by a test run.
daemon_module.LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "test-daemon.log")

# Redirect BOTH files the daemon writes, not just the state. /register,
# /projects and the settings endpoint all end in cfgmod.save(), which writes
# cfgmod.PATH — pointed at the real config.json this suite would overwrite the
# live bot token and the project list with its own fixtures.
cfgmod.PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "test-config.json")
cfgmod.STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "test-state.json")

# A fake path on purpose: the tests never touch it on disk, and hard-coding a
# real home directory would tie the suite to one machine.
PROJECT = "C:/Work/BridgeProject"
TOPIC = 42


class FakeBot:
    def __init__(self, *_, **__):
        self.sent = []
        self.edits = []
        self.callbacks = []
        self.next_id = 1000

    def create_topic(self, chat_id, name):
        self.callbacks.append(("create_topic", chat_id, name))
        return {"message_thread_id": TOPIC}

    def send_message(self, chat_id, text, thread_id=None, markup=None,
                     html=True, silent=False):
        self.next_id += 1
        self.sent.append({"id": self.next_id, "text": text, "thread": thread_id,
                          "markup": markup, "silent": silent})
        return {"message_id": self.next_id}

    def edit_message(self, chat_id, message_id, text, markup=None, html=True):
        self.edits.append({"id": message_id, "text": text})
        return {}

    def pin_message(self, *a, **k):
        return {}

    def answer_callback(self, cid, text=None):
        self.callbacks.append(text)

    def get_me(self):
        return {"username": "fake"}


def make_daemon(tmp_state, away):
    cfg = dict(cfgmod.DEFAULTS)
    cfg.update({"bot_token": "x", "chat_id": -100, "wait_seconds": 5,
                # Off unless a test is about it: otherwise every at-the-PC
                # stop would wait on whether somebody is touching the mouse
                # of the machine running the suite.
                "stop_grace": {"enabled": False},
                # Off by default so tests never depend on a real usage.json
                # lying around on disk.
                "usage_report": {"enabled": False},
                "projects": {cfgmod.normalize(PROJECT): {"name": "TGbotClaude"}}})
    cfgmod.STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     tmp_state)
    d = Daemon.__new__(Daemon)
    d.cfg = cfg
    d.bot = FakeBot()
    d.state = {"topics": {}, "away": away, "offset": None, "status_message_id": None}
    d.lock = threading.RLock()
    d.tail_lock = threading.Lock()
    d.sessions, d.waiters, d.by_topic, d.queue = {}, {}, {}, {}
    d.spawning = set()
    d.persist = lambda: None
    return d


def evt(name, **extra):
    base = {"hook_event_name": name, "cwd": PROJECT, "session_id": "s1",
            "transcript_path": None}
    base.update(extra)
    return base


def run_async(d, event):
    box = {}
    t = threading.Thread(target=lambda: box.update(result=d.handle_event(event)))
    t.start()
    return box, t


def wait_for(predicate, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_foreign_project_is_ignored():
    d = make_daemon("s.json", away=True)
    assert d.handle_event(evt("Stop", cwd="c:/some/other/place")) == {}
    assert d.bot.sent == [], "must not touch Telegram outside registered projects"
    print("PASS foreign project ignored")


def test_present_mode_does_not_block():
    d = make_daemon("s.json", away=False)
    start = time.time()
    out = d.handle_event(evt("Stop"))
    assert out == {}, out
    assert time.time() - start < 1, "at-PC mode must never block the session"
    assert d.bot.sent and d.bot.sent[-1]["silent"], "log messages must be silent"
    print("PASS present mode is a silent log")


def test_away_stop_waits_and_continues():
    d = make_daemon("s.json", away=True)
    box, t = run_async(d, evt("Stop"))
    assert wait_for(lambda: d.by_topic.get(TOPIC)), "waiter never registered"
    assert not d.bot.sent[-1]["silent"], "away mode must notify"
    d.deliver_text("почини тесты", TOPIC,
                   {"chat": {"id": -100}, "message_thread_id": TOPIC})
    t.join(5)
    assert box["result"] == {"decision": "block", "reason": "почини тесты"}, box
    print("PASS away Stop resumes with the Telegram task")


def test_away_stop_timeout_releases():
    d = make_daemon("s.json", away=True)
    d.cfg["wait_seconds"] = 1
    start = time.time()
    out = d.handle_event(evt("Stop"))
    assert out == {}, out
    assert 1 <= time.time() - start < 4
    assert not d.waiters, "waiter must be cleaned up after a timeout"
    print("PASS timeout releases the session cleanly")


def test_askuserquestion_buttons():
    d = make_daemon("s.json", away=True)
    questions = [{"question": "Какой стек?", "header": "Стек", "multiSelect": False,
                  "options": [{"label": "Python", "description": "stdlib"},
                              {"label": "Node", "description": "grammY"}]}]
    box, t = run_async(d, evt("PreToolUse", tool_name="AskUserQuestion",
                              tool_input={"questions": questions}))
    assert wait_for(lambda: d.by_topic.get(TOPIC)), "question never posted"
    wid = d.by_topic[TOPIC]
    markup = d.bot.sent[-1]["markup"]
    assert markup and len(markup["inline_keyboard"]) == 2, markup
    d.on_callback({"id": "cb1", "data": f"a:{wid}:0:1"})
    t.join(5)
    reason = box["result"]["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Node" in reason and "Какой стек?" in reason, reason
    assert box["result"]["hookSpecificOutput"]["permissionDecision"] == "deny"
    print("PASS AskUserQuestion answered by button")


def test_multiselect_needs_done():
    d = make_daemon("s.json", away=True)
    questions = [{"question": "Что слать?", "multiSelect": True,
                  "options": [{"label": "Итоги", "description": ""},
                              {"label": "Ошибки", "description": ""}]}]
    box, t = run_async(d, evt("PreToolUse", tool_name="AskUserQuestion",
                              tool_input={"questions": questions}))
    assert wait_for(lambda: d.by_topic.get(TOPIC))
    wid = d.by_topic[TOPIC]
    d.on_callback({"id": "c", "data": f"m:{wid}:0:0"})
    d.on_callback({"id": "c", "data": f"m:{wid}:0:1"})
    assert not box, "multi-select must stay open until Готово"
    d.on_callback({"id": "c", "data": f"d:{wid}:0"})
    t.join(5)
    reason = box["result"]["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Итоги, Ошибки" in reason, reason
    print("PASS multi-select waits for Готово")


def test_queue_when_nobody_listens():
    d = make_daemon("s.json", away=True)
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.deliver_text("собери релиз", TOPIC,
                   {"chat": {"id": -100}, "message_thread_id": TOPIC})
    assert d.queue[cfgmod.normalize(PROJECT)] == ["собери релиз"]
    out = d.handle_event(evt("Stop"))
    assert out["decision"] == "block" and "собери релиз" in out["reason"], out
    assert not d.queue.get(cfgmod.normalize(PROJECT)), "queue must drain"
    print("PASS queued task is delivered on the next Stop")


def test_task_reaches_a_turn_already_running():
    """The whole point: a correction must not wait for the work to finish."""
    d = make_daemon("s.json", away=True)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key] = TOPIC
    d.sessions["s1"] = {"project": key, "name": "TGbotClaude", "alive": True,
                        "last_seen": time.time(), "parked": False}

    d.deliver_text("стой, не трогай config.json", TOPIC,
                   {"chat": {"id": -100}, "message_thread_id": TOPIC})
    assert d.queue[key] == ["стой, не трогай config.json"]
    assert d.bot.callbacks == [] and d.bot.sent, "the message must be acknowledged"

    out = d.handle_event(evt("PostToolBatch"))
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolBatch", out
    assert "не трогай config.json" in context, context
    assert not d.queue.get(key), "handed over, so it must not arrive twice"
    assert any("Передал" in m["text"] or "Handed" in m["text"]
               for m in d.bot.sent), d.bot.sent
    print("PASS a task typed mid-turn reaches the running session")


def test_a_switch_flipped_late_still_reaches_the_session():
    """The turn is over and the switch comes after it. Same session, no agent."""
    d = make_daemon("s.json", away=False)
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.cfg["stop_grace"] = {"enabled": True, "seconds": 10}
    d.refresh_status = lambda: None
    untouched = daemon_module.input_idle_seconds
    daemon_module.input_idle_seconds = lambda: 999.0    # nobody at the desk
    try:
        box, thread = run_async(d, evt("Stop"))
        time.sleep(0.5)                                 # the turn is being held
        assert not box, "the hook must still be there when the switch arrives"
        d.set_away(True)
        assert wait_for(lambda: d.waiters), "the held turn must become a wait"
        wid = next(iter(d.waiters))
        d.on_callback({"id": "cb", "data": f"go:{wid}"})
        thread.join(3)
        assert box["result"]["decision"] == "block", box
    finally:
        daemon_module.input_idle_seconds = untouched
    print("PASS a switch flipped after the turn still lands in that session")


def test_a_turn_that_ends_at_the_desk_is_not_held():
    """Working at the keyboard must not notice this exists."""
    d = make_daemon("s.json", away=False)
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.cfg["stop_grace"] = {"enabled": True, "seconds": 30}
    untouched = daemon_module.input_idle_seconds
    # Touched a moment ago, and the turn ran for a while before that: the
    # touch is newer than the wait, which is what "still here" means.
    daemon_module.input_idle_seconds = lambda: 0.0
    try:
        start = time.time()
        assert d.handle_event(evt("Stop")) == {}
        assert time.time() - start < 3, "a working desk must not be held up"
    finally:
        daemon_module.input_idle_seconds = untouched
    print("PASS a turn ending at a busy desk is let go at once")


def test_a_finished_turn_is_not_promised_a_next_step():
    """The turn ended at the keyboard, then control came over. Nothing runs.

    Reproduces the live sequence: Stop answered at 12:04:59 with nobody away,
    away went on after it, and the task typed next was told a working session
    would take it at its next step.
    """
    d = make_daemon("s.json", away=False)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key] = TOPIC
    d.refresh_status = lambda: None
    d.handle_event(evt("UserPromptSubmit"))
    assert d.working_session(key), "a turn is running"
    d.handle_event(evt("Stop"))          # ends at the PC: no waiter, no block
    assert not d.working_session(key), "the turn is over"

    d.set_away(True)
    assert any("⏸" in m["text"] for m in d.bot.sent), d.bot.sent
    # Out of reach is not abandoned. Parking it here is what sent a message
    # meant for the window in the editor to a second agent in its directory.
    assert not d.sessions["s1"].get("parked"), "a minute of quiet is not a goodbye"
    assert d.live_session(key), "an open editor must keep an agent away"

    d.deliver_text("Тест 1", TOPIC,
                   {"chat": {"id": -100}, "message_thread_id": TOPIC})
    assert d.queue[key] == ["Тест 1"]
    said = d.bot.sent[-1]["text"]
    assert i18n.t("ack.queued") in said, said
    assert i18n.t("ack.handoff") not in said, "promised a step that is not coming"
    print("PASS an idle session is never promised a step it does not have")


def test_idle_batch_says_nothing():
    """This runs on every batch of every turn; empty must stay free."""
    d = make_daemon("s.json", away=True)
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    assert d.handle_event(evt("PostToolBatch")) == {}
    assert d.bot.sent == [], "an empty queue must not produce chatter"
    print("PASS an empty queue costs the turn nothing")


def test_hold_releases_the_wait_in_silence():
    """Waiting is not answering: the session must hear nothing at all."""
    d = make_daemon("s.json", away=True)
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    box, thread = run_async(d, evt("Stop"))
    assert wait_for(lambda: d.waiters), "the away stop must offer its buttons"
    wid = next(iter(d.waiters))

    markup = next(m["markup"] for m in reversed(d.bot.sent) if m.get("markup"))
    buttons = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert f"hold:{wid}" in buttons, buttons

    d.on_callback({"id": "cb", "data": f"hold:{wid}"})
    thread.join(3)
    assert box["result"] == {}, "nothing may be handed back to the session"
    print("PASS holding releases the hook without a word to the session")


def test_todos_edit_one_message():
    d = make_daemon("s.json", away=False)
    d.cfg["log_when_present"] = ["todo"]  # this test is about rendering, not filtering
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC

    def todo_event(statuses):
        todos = [{"content": f"шаг {i}", "status": s, "activeForm": f"делаю {i}"}
                 for i, s in enumerate(statuses)]
        return evt("PostToolUse", tool_name="TodoWrite", tool_input={"todos": todos})

    d.handle_event(todo_event(["in_progress", "pending", "pending"]))
    assert len(d.bot.sent) == 1, "first checklist must be a new message"
    first = d.bot.sent[0]
    assert first["silent"], "progress updates must never buzz the phone"
    assert "0/3" in first["text"] and "<b>шаг 0</b>" in first["text"], first["text"]

    d.handle_event(todo_event(["completed", "in_progress", "pending"]))
    assert len(d.bot.sent) == 1, "second update must edit, not post again"
    assert len(d.bot.edits) == 1, d.bot.edits
    edited = d.bot.edits[0]["text"]
    assert edited.startswith("📋") and "1/3" in edited, edited
    assert "<s>шаг 0</s>" in edited, edited

    d.handle_event(evt("PostToolUse", tool_name="Bash", tool_input={"command": "ls"}))
    assert len(d.bot.sent) == 1 and len(d.bot.edits) == 1, "other tools must be ignored"
    print("PASS todos update one message in place")


def test_todos_forget_on_session_end():
    d = make_daemon("s.json", away=False)
    d.cfg["log_when_present"] = ["todo", "session"]
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.handle_event(evt("PostToolUse", tool_name="TodoWrite",
                       tool_input={"todos": [{"content": "a", "status": "pending"}]}))
    assert d.state["todo_msgs"].get("s1")
    d.handle_event(evt("SessionEnd"))
    assert not d.state["todo_msgs"].get("s1"), "stale message id must not survive"
    print("PASS todo message is forgotten when the session ends")


def test_tool_failures_can_be_switched_off():
    """The ❌ Bash/Read dumps are noise for some workflows; the switch must
    silence them in both modes while still counting them for the digest."""
    for away in (False, True):
        d = make_daemon("s.json", away=away)
        d.cfg["report_tool_failures"] = False
        d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
        d.handle_event(evt("PostToolUseFailure", tool_name="Bash",
                           tool_input={"command": "javac X.java"},
                           tool_response={"stderr": "ClassNotFoundException"}))
        assert d.bot.sent == [], f"away={away}: failure report must stay off"
        stats = d.state["stats"][time.strftime("%Y-%m-%d")]
        assert stats[cfgmod.normalize(PROJECT)]["failures"] == 1, "still counted"
    print("PASS tool failure reports can be turned off without losing the count")


def test_live_text_at_the_pc_is_silent_and_opt_in():
    """With the "live" kind on, the stream arrives at the PC — silently."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flow.jsonl")
    open(path, "w", encoding="utf-8").close()
    d = make_daemon("s.json", away=False)
    d.cfg["log_when_present"] = ["live"]    # stream on, the rest muted
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.handle_event(evt("UserPromptSubmit", transcript_path=path))
    _append(path, text="Живой блок текста.")
    d.tail_once()
    assert len(d.bot.sent) == 1, d.bot.sent
    assert "Живой блок текста." in d.bot.sent[0]["text"]
    assert d.bot.sent[0]["silent"], "at the PC it must be silent, not loud"
    os.remove(path)
    print("PASS live text at the PC is silent and governed by the live kind")


def test_failure_message_is_informative():
    d = make_daemon("s.json", away=False)
    d.cfg["report_tool_failures"] = True
    d.cfg["log_when_present"] = ["failure"]
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.handle_event(evt("PostToolUseFailure", tool_name="Bash",
                       tool_input={"command": "pytest -q"},
                       tool_response={"stderr": "ModuleNotFoundError: no module named x"}))
    text = d.bot.sent[-1]["text"]
    assert "pytest -q" in text, "must show what was attempted"
    assert "ModuleNotFoundError" in text, "must show why it failed"
    print("PASS failure message names the command and the error")


def test_failure_without_detail_says_so():
    d = make_daemon("s.json", away=False)
    d.cfg["report_tool_failures"] = True
    d.cfg["log_when_present"] = ["failure"]
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.handle_event(evt("PostToolUseFailure", tool_name="Bash", tool_input={},
                       tool_response={}))
    text = d.bot.sent[-1]["text"]
    assert "{}" not in text and "подробност" in text, text
    print("PASS empty failure payload degrades to a readable line")


def test_utf8_survives_the_hook_client():
    """Regression: Windows stdin defaults to cp1251 and mangled Cyrillic."""
    import subprocess
    payload = json.dumps({"hook_event_name": "Stop", "cwd": "c:/nowhere/at/all",
                          "session_id": "enc", "note": "Собрать мост ёжик"},
                         ensure_ascii=False).encode("utf-8")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run([sys.executable, os.path.join(root, "hookc.py")],
                          input=payload, capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert b"UnicodeDecodeError" not in proc.stderr, proc.stderr
    print("PASS hook client reads UTF-8 stdin without mangling")


def test_question_renders_as_real_formatting():
    """Regression: the body was built as HTML, then escaped into visible tags."""
    d = make_daemon("s.json", away=True)
    q = {"question": "Какой стек?", "multiSelect": False,
         "options": [{"label": "Python", "description": "stdlib, без зависимостей"},
                     {"label": "Node", "description": "grammY"}]}
    joined = "\n".join(d.render_question(q, 0, 1, "Proj"))
    assert "&lt;b&gt;" not in joined and "&lt;i&gt;" not in joined, joined
    assert "<b>Какой стек?</b>" in joined, joined
    assert "<b>Python</b>" in joined, joined
    print("PASS question body reaches Telegram as formatting, not tags")


def test_markdown_table_becomes_monospace():
    from claudetg.render import render
    md = ("Итог:\n\n"
          "| Компонент | Статус |\n|---|---|\n| рендер | готов |\n| демон | готов |\n\n"
          "Дальше — автозапуск.")
    out = "\n".join(render(md))
    assert "<pre>" in out, out
    assert "|" not in out.split("<pre>")[1].split("</pre>")[0], "pipes must be gone"
    assert "Компонент" in out and "Дальше" in out
    print("PASS markdown table renders as aligned monospace")


def test_transcript_keeps_whole_turn():
    import datetime as dt
    from claudetg.transcript import summarize
    t0 = dt.datetime(2026, 7, 30, 12, 0, 0, tzinfo=dt.timezone.utc)

    def row(kind, text, offset, tool_result=False):
        content = ([{"type": "tool_result", "content": "x"}] if tool_result
                   else [{"type": "text", "text": text}])
        return {"type": kind, "timestamp": (t0 + dt.timedelta(seconds=offset))
                .isoformat().replace("+00:00", "Z"),
                "message": {"content": content}}

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "turn.jsonl")
    rows = [
        row("user", "первый вопрос", 0),
        row("assistant", "старый ход — не должен попасть", 5),
        row("user", "второй вопрос", 10),
        row("assistant", "Смотрю логи.", 12),
        row("user", "", 13, tool_result=True),
        row("assistant", "Нашёл причину: кодировка.", 20),
        row("assistant", "**Итог:** починено.", 30),
    ]
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    text, seconds = summarize(path)
    os.remove(path)
    assert "старый ход" not in text, text
    assert "Смотрю логи." in text and "Нашёл причину" in text and "Итог" in text, text
    assert seconds == 20, seconds
    print("PASS transcript returns the whole turn, not just the last line")


def _status_payload(indicator="none", api="operational", incidents=()):
    return {
        "status": {"indicator": indicator, "description": "desc"},
        "components": [{"name": "Claude API (api.anthropic.com)", "status": api},
                       {"name": "Claude Code", "status": "operational"}],
        "incidents": list(incidents),
    }


def test_status_first_poll_is_silent():
    from claudetg import status
    snap = status.snapshot(_status_payload())
    assert status.diff(None, snap) == [], "baseline poll must not announce anything"
    assert status.diff(snap, snap) == [], "unchanged status must stay quiet"
    print("PASS status monitor stays quiet until something changes")


def test_status_reports_outage_and_recovery():
    from claudetg import status
    calm = status.snapshot(_status_payload())
    incident = {"id": "i1", "name": "Elevated API errors", "status": "investigating",
                "impact": "major", "shortlink": "https://stspg.io/x",
                "incident_updates": [{"body": "Мы изучаем рост ошибок."}]}
    broken = status.snapshot(_status_payload("major", "major_outage", [incident]))

    messages = "\n".join(status.diff(calm, broken))
    assert "Claude API" in messages and "крупный сбой" in messages, messages
    assert "Elevated API errors" in messages, messages
    assert "Мы изучаем рост ошибок." in messages, messages

    recovered = "\n".join(status.diff(broken, calm))
    assert "инцидент закрыт" in recovered, recovered
    assert "работает" in recovered, recovered
    print("PASS status monitor reports outage details and recovery")


def test_present_mode_event_filter():
    d = make_daemon("s.json", away=False)
    d.cfg["log_when_present"] = ["stop"]
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.handle_event(evt("PostToolUse", tool_name="TodoWrite",
                       tool_input={"todos": [{"content": "a", "status": "pending"}]}))
    d.handle_event(evt("SessionEnd"))
    assert d.bot.sent == [], "muted kinds must not reach Telegram at the PC"

    d.state["away"] = True
    d.handle_event(evt("SessionEnd"))
    assert d.bot.sent, "away mode must ignore the filter"
    print("PASS at-PC filter mutes only the kinds you excluded")


def test_watchdog_fires_once_per_stall():
    d = make_daemon("s.json", away=True)
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.handle_event(evt("UserPromptSubmit"))
    d.check_stalls(limit=9999)
    assert d.bot.sent == [], "a fresh turn is not a stall"

    d.sessions["s1"]["turn_started"] = time.time() - 1800
    d.check_stalls(limit=600)
    assert len(d.bot.sent) == 1 and "🐌" in d.bot.sent[-1]["text"], d.bot.sent
    assert not d.bot.sent[-1]["silent"], "a stall should actually notify"

    d.check_stalls(limit=600)
    assert len(d.bot.sent) == 1, "must not repeat the same alert every 30s"

    d.handle_event(evt("Stop"))
    assert d.sessions["s1"]["turn_started"] is None, "Stop must close the turn"
    print("PASS watchdog alerts once and resets on Stop")


def test_stop_failure_reports_api_error():
    d = make_daemon("s.json", away=False)
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.handle_event(evt("UserPromptSubmit"))
    d.handle_event(evt("StopFailure", error="529 overloaded_error: API temporarily down"))
    text = d.bot.sent[-1]["text"]
    assert "529" in text and "сбой API" in text, text
    assert not d.bot.sent[-1]["silent"], "API outages must notify"
    assert d.sessions["s1"]["turn_started"] is None, "a dead turn must not stall-alert"
    print("PASS StopFailure reports the API error and closes the turn")


def test_daily_digest_counts_work():
    d = make_daemon("s.json", away=False)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key] = TOPIC
    d.state["topics"][d.STATUS_KEY] = 99
    d.bump_stat(key, "turns", 65)
    d.bump_stat(key, "turns", 35)
    d.bump_stat(key, "failures")
    d.send_digest(time.strftime("%Y-%m-%d"))
    text = d.bot.sent[-1]["text"]
    assert "TGbotClaude" in text and "Сводка" in text, text
    assert "2" in text and "1м 40с" in text, text
    print("PASS daily digest tallies turns, failures and time")


def test_git_summary_skips_non_git():
    d = make_daemon("s.json", away=False)
    assert d.git_summary("c:/definitely/not/a/repo") == ""
    assert d.git_summary(None) == ""
    print("PASS git summary stays silent outside a repository")


def test_extra_paths_share_one_topic():
    """Working dirs that belong to a project must not spawn their own chat."""
    cfg = dict(cfgmod.DEFAULTS)
    cfg["projects"] = {
        cfgmod.normalize("C:/Users/Me/Desktop/ReverseL2"): {
            "name": "ReverseL2",
            "extra_paths": ["C:/Users/Me/Desktop/core", "E:/563 client/system"]},
        cfgmod.normalize("E:/Convert_lic30-lic40"): {"name": "Convert"},
    }
    for cwd, expected in [
        ("C:/Users/Me/Desktop/ReverseL2", "ReverseL2"),
        ("C:/Users/Me/Desktop/ReverseL2/uc_source", "ReverseL2"),
        ("C:/Users/Me/Desktop/core", "ReverseL2"),
        ("C:/Users/Me/Desktop/core/deep/inside", "ReverseL2"),
        ("E:/563 client/system", "ReverseL2"),
        ("E:/Convert_lic30-lic40/tools", "Convert"),
    ]:
        match = cfgmod.project_for(cfg, cwd)
        assert match and match[1]["name"] == expected, f"{cwd} -> {match}"
        assert match[0] == cfgmod.normalize(
            "C:/Users/Me/Desktop/ReverseL2" if expected == "ReverseL2"
            else "E:/Convert_lic30-lic40"), "must resolve to the primary key"
    assert cfgmod.project_for(cfg, "C:/Users/Me/Desktop/Other") is None
    print("PASS extra paths resolve to the owning project")


def test_usage_report_formats_limits():
    from claudetg import usage
    now = time.time()
    data = {"has_limits": True, "captured_at": now,
            "rate_limits": {"five_hour": {"used_percentage": 42.7,
                                          "resets_at": now + 3600 * 2 + 900},
                            "seven_day": {"used_percentage": 78.0,
                                          "resets_at": now + 86400 * 2}}}
    body = usage.report(data)
    assert "43%" in body and "78%" in body, body
    assert "5-часовое окно" in body and "Недельное" in body, body
    assert "через 2ч 15м" in body, body
    assert "обнуление" in body, body
    print("PASS usage report shows percentages and reset times")


def test_release_returns_control_to_vscode():
    """Sitting back down must not leave a hook blocked on a chat you left."""
    d = make_daemon("s.json", away=True)
    box, t = run_async(d, evt("Stop"))
    assert wait_for(lambda: d.by_topic.get(TOPIC)), "waiter never registered"
    d.set_away(False)
    t.join(5)
    assert box["result"] == {}, box
    assert not d.waiters, "waiters must be cleared on release"

    d.state["away"] = True
    box2, t2 = run_async(d, evt("PreToolUse", tool_name="AskUserQuestion",
                                tool_input={"questions": [
                                    {"question": "?", "options": [
                                        {"label": "a", "description": ""}]}]}))
    assert wait_for(lambda: d.by_topic.get(TOPIC))
    assert d.release_all() == 1
    t2.join(5)
    assert box2["result"] == {}, "an unanswered question must fall back to VSCode"
    print("PASS switching back to the PC releases every blocked hook")


def test_usage_ignores_stale_or_empty_cache(tmp="usage_probe.json"):
    from claudetg import usage
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"has_limits": False, "captured_at": time.time(),
                   "rate_limits": {}}, f)
    assert usage.load(tmp) is None, "a version without rate_limits must be ignored"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"has_limits": True, "captured_at": time.time() - 99999,
                   "rate_limits": {"five_hour": {"used_percentage": 1}}}, f)
    assert usage.load(tmp, max_age=3600) is None, "stale numbers must not be shown"
    os.remove(tmp)
    assert usage.load("no_such_file.json") is None
    print("PASS usage cache is ignored when empty, stale or missing")


def _fake_transcript(tmpdir, root_cwd):
    os.makedirs(tmpdir, exist_ok=True)
    path = os.path.join(tmpdir, "sess.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "cwd": root_cwd,
                            "message": {"content": "hi"}}, ensure_ascii=False) + "\n")
    return path


def test_auto_discovery_creates_one_project_per_transcript_dir():
    import shutil
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_auto")
    shutil.rmtree(base, ignore_errors=True)
    t1 = _fake_transcript(os.path.join(base, "e--Convert_lic30-lic40"),
                          "E:\\Convert_lic30-lic40")
    t2 = _fake_transcript(os.path.join(base, "c--Desktop-Other"),
                          "C:\\Users\\Me\\Desktop\\Other")

    d = make_daemon("s.json", away=False)
    d.cfg["projects"] = {}
    d.cfg["auto_discover"] = True
    d.state["auto_names"] = {}

    # Same session wandering into a subdirectory must not spawn a new project.
    a = d.resolve_project({"session_id": "s1", "cwd": "E:/Convert_lic30-lic40",
                           "transcript_path": t1})
    b = d.resolve_project({"session_id": "s2", "cwd": "E:/Convert_lic30-lic40/tools/x",
                           "transcript_path": t1})
    c = d.resolve_project({"session_id": "s3", "cwd": "C:/Users/Me/Desktop/Other",
                           "transcript_path": t2})
    assert a[0] == b[0], "one project per transcript dir"
    assert a[1] == "Convert_lic30-lic40", a
    assert c[1] == "Other" and c[0] != a[0], c
    shutil.rmtree(base, ignore_errors=True)
    print("PASS auto-discovery names projects and never splits one in two")


def test_manual_entry_overrides_auto_name():
    import shutil
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_auto2")
    shutil.rmtree(base, ignore_errors=True)
    t = _fake_transcript(os.path.join(base, "enc"), "C:\\Users\\Me\\Desktop\\ReverseL2")
    d = make_daemon("s.json", away=False)
    d.cfg["projects"] = {cfgmod.normalize("C:/Users/Me/Desktop/ReverseL2"):
                         {"name": "ReverseL2 (боевой)"}}
    d.cfg["auto_discover"] = True
    key, name = d.resolve_project({"session_id": "s1",
                                   "cwd": "C:/Users/Me/Desktop/ReverseL2",
                                   "transcript_path": t})
    assert name == "ReverseL2 (боевой)", name
    assert key == cfgmod.normalize("C:/Users/Me/Desktop/ReverseL2"), key
    shutil.rmtree(base, ignore_errors=True)
    print("PASS explicit project entries still win over auto-discovery")


def test_exclude_paths_beat_auto_discovery():
    import shutil
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_auto3")
    shutil.rmtree(base, ignore_errors=True)
    t = _fake_transcript(os.path.join(base, "enc"), "E:\\scratch\\throwaway")
    d = make_daemon("s.json", away=False)
    d.cfg["projects"] = {}
    d.cfg["auto_discover"] = True
    d.cfg["exclude_paths"] = ["E:/scratch"]
    assert d.resolve_project({"session_id": "s1", "cwd": "E:/scratch/throwaway",
                              "transcript_path": t}) is None
    assert d.handle_event(evt("Stop", cwd="E:/scratch/throwaway",
                              transcript_path=t)) == {}
    assert d.bot.sent == [], "excluded projects must never reach Telegram"
    shutil.rmtree(base, ignore_errors=True)
    print("PASS exclude_paths still keeps projects out")


def test_auto_discovery_off_restores_whitelist():
    d = make_daemon("s.json", away=False)
    d.cfg["auto_discover"] = False
    assert d.resolve_project({"session_id": "s1", "cwd": "C:/somewhere/else",
                              "transcript_path": "x/y/z.jsonl"}) is None
    print("PASS auto_discover=false brings back the strict whitelist")


def test_picker_lists_and_marks_enabled():
    import shutil
    from claudetg import discover
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_home")
    shutil.rmtree(base, ignore_errors=True)
    _fake_transcript(os.path.join(base, "enc-a"), "C:\\Work\\Alpha")
    _fake_transcript(os.path.join(base, "enc-b"), "C:\\Work\\Beta")
    cfg = {"projects": {cfgmod.normalize("C:/Work/Alpha"): {"name": "Alpha"}}}

    listing = discover.list_projects(home=base, cfg=cfg)
    by_name = {p["name"]: p for p in listing}
    assert by_name["Alpha"]["enabled"] and not by_name["Beta"]["enabled"], listing
    assert by_name["Beta"]["root"] == "C:/Work/Beta", by_name["Beta"]
    shutil.rmtree(base, ignore_errors=True)
    print("PASS picker lists known projects and marks the connected ones")


def test_selection_keeps_extra_paths():
    """Toggling a project must not silently discard hand-bound directories."""
    from claudetg import discover
    key = cfgmod.normalize("C:/Work/Alpha")
    cfg = {"projects": {key: {"name": "Alpha (боевой)",
                              "extra_paths": ["C:/Work/data"]}}}
    discover.apply_selection(cfg, [key])
    kept = list(cfg["projects"].values())[0]
    assert kept["extra_paths"] == ["C:/Work/data"], cfg
    assert kept["name"] == "Alpha (боевой)", cfg

    discover.apply_selection(cfg, [])
    assert cfg["projects"] == {}, "deselecting must remove the project"
    print("PASS selection preserves custom names and extra paths")


def test_toggle_adds_and_removes():
    from claudetg import discover
    a, b = cfgmod.normalize("C:/Work/Alpha"), cfgmod.normalize("C:/Work/Beta")
    cfg = {"projects": {}}
    discover.apply_selection(cfg, [a])
    assert set(map(cfgmod.normalize, cfg["projects"])) == {a}
    discover.apply_selection(cfg, [a, b])
    assert set(map(cfgmod.normalize, cfg["projects"])) == {a, b}
    discover.apply_selection(cfg, [b])
    assert set(map(cfgmod.normalize, cfg["projects"])) == {b}
    print("PASS toggling a project adds and removes it cleanly")


def _append(path, **fields):
    entry = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": fields.pop("text", "")}]}}
    entry.update(fields)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def test_live_messages_stream_as_they_appear():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live.jsonl")
    open(path, "w", encoding="utf-8").close()
    _append(path, text="это было до подключения")

    d = make_daemon("s.json", away=False)
    d.cfg["live_messages"] = True
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.handle_event(evt("UserPromptSubmit", transcript_path=path))
    d.tail_once()
    assert d.bot.sent == [], "history before the session must not be replayed"

    _append(path, text="Первый блок.")
    d.tail_once()
    assert len(d.bot.sent) == 1 and "Первый блок." in d.bot.sent[-1]["text"]

    d.tail_once()
    assert len(d.bot.sent) == 1, "nothing new must not resend"

    _append(path, text="из субагента", isSidechain=True)
    _append(path, text="Второй блок.")
    d.tail_once()
    assert len(d.bot.sent) == 2, d.bot.sent
    assert "Второй блок." in d.bot.sent[-1]["text"]
    assert not any("субагент" in m["text"] for m in d.bot.sent), "subagents excluded"
    os.remove(path)
    print("PASS live messages stream once, skipping history and subagents")


def test_partial_line_is_not_sent():
    """A half-written JSONL line must wait, not be dropped or mangled."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "partial.jsonl")
    open(path, "w", encoding="utf-8").close()
    d = make_daemon("s.json", away=False)
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.handle_event(evt("UserPromptSubmit", transcript_path=path))

    fragment = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Половина"}]}}, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(fragment[:20])
    d.tail_once()
    assert d.bot.sent == [], "partial line must not be emitted"

    with open(path, "a", encoding="utf-8") as f:
        f.write(fragment[20:] + "\n")
    d.tail_once()
    assert len(d.bot.sent) == 1 and "Половина" in d.bot.sent[-1]["text"]
    os.remove(path)
    print("PASS a partially written line waits until it is complete")


def test_live_stream_muted_at_the_pc_by_filter():
    """Without the "live" kind the stream is silent at the PC and the tail
    accrues: switching away must deliver everything written meanwhile."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mute.jsonl")
    open(path, "w", encoding="utf-8").close()
    d = make_daemon("s.json", away=False)
    d.cfg["live_messages"] = True
    d.cfg["log_when_present"] = ["stop"]
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.handle_event(evt("UserPromptSubmit", transcript_path=path))

    _append(path, text="Тихий блок.")
    d.tail_once()
    assert d.bot.sent == [], "live text must stay muted at the PC"

    d.state["away"] = True          # user left: the accrued tail flows
    d.tail_once()
    assert len(d.bot.sent) == 1 and "Тихий блок." in d.bot.sent[-1]["text"]
    os.remove(path)
    print("PASS muted live stream accrues at the PC and flows once away")


def test_muted_turn_is_settled_by_stop_not_replayed():
    """At the PC with live muted the closing carries the text; a later switch
    to away must not replay the finished turn."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mute2.jsonl")
    open(path, "w", encoding="utf-8").close()
    d = make_daemon("s.json", away=False)
    d.cfg["live_messages"] = True
    d.cfg["log_when_present"] = ["stop"]
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.handle_event(evt("UserPromptSubmit", transcript_path=path))

    _append(path, text="Текст завершённого хода.")
    d.handle_event(evt("Stop", transcript_path=path))
    closing = "\n".join(m["text"] for m in d.bot.sent)
    assert "Текст завершённого хода." in closing, d.bot.sent

    before = len(d.bot.sent)
    d.state["away"] = True
    d.tail_once()
    assert len(d.bot.sent) == before, "finished turn must not be replayed"
    os.remove(path)
    print("PASS a muted turn is settled by its closing and never replayed")


def test_closing_repeats_text_unless_it_was_streamed():
    d = make_daemon("s.json", away=False)
    d.cfg["live_messages"] = True
    head = render.header("✅", "P", 107)

    assert d.closing("длинный текст хода", head, streamed=True) == \
        ["✅ <b>P</b> · 1м 47с"], "streamed text must not be repeated"
    # Regression: a daemon restarted mid-turn streams nothing, and the turn's
    # text must still arrive instead of a lonely header.
    assert "длинный текст хода" in d.closing("длинный текст хода", head,
                                             streamed=False)[0]
    d.cfg["live_messages"] = False
    assert "длинный текст хода" in d.closing("длинный текст хода", head,
                                             streamed=True)[0]
    print("PASS closing never drops text that was not streamed")


def test_any_event_registers_the_session():
    """A session whose turn started before the hooks existed must still become
    visible — otherwise it is silent until it happens to finish."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "midturn.jsonl")
    open(path, "w", encoding="utf-8").close()
    d = make_daemon("s.json", away=False)
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC

    assert d.sessions == {}
    d.handle_event(evt("PostToolUse", tool_name="TodoWrite", transcript_path=path,
                       tool_input={"todos": [{"content": "x", "status": "pending"}]}))
    assert "s1" in d.sessions, "a mid-turn event must register the session"
    assert d.sessions["s1"]["name"] == "TGbotClaude"
    assert d.sessions["s1"].get("tail_path") == path, "streaming must start too"

    _append(path, text="Блок после подключения.")
    d.tail_once()
    assert any("Блок после подключения." in m["text"] for m in d.bot.sent), d.bot.sent

    d.handle_event(evt("SessionEnd", transcript_path=path))
    assert "s1" not in d.sessions, "SessionEnd must still remove it"
    os.remove(path)
    print("PASS any event registers the session and starts streaming")


def test_settings_toggles_round_trip():
    d = make_daemon("s.json", away=False)
    saved = {}
    cfgmod.save = lambda cfg, path=None: saved.update(cfg)

    paths = {t["path"] for t in d.settings_snapshot()}
    assert {"live_messages", "report_tool_failures", "log_when_present.todo",
            "status_monitor.enabled"} <= paths, paths

    d.apply_settings({"live_messages": False, "log_when_present.todo": True,
                      "status_monitor.enabled": False})
    assert d.cfg["live_messages"] is False
    assert "todo" in d.cfg["log_when_present"]
    assert d.cfg["status_monitor"]["enabled"] is False
    assert saved, "changes must be written to config.json"

    d.apply_settings({"log_when_present.todo": False})
    assert "todo" not in d.cfg["log_when_present"]
    # A nested flag must not wipe its siblings.
    assert "interval_seconds" in d.cfg["status_monitor"], d.cfg["status_monitor"]
    print("PASS widget toggles read and write every setting")


def test_unknown_setting_is_ignored():
    d = make_daemon("s.json", away=False)
    cfgmod.save = lambda cfg, path=None: None
    d.apply_settings({"bot_token": "стереть", "projects": {}})
    assert d.cfg["bot_token"] == "x", "only declared toggles may be written"
    assert d.cfg["projects"], "unrelated config must survive"
    print("PASS the settings endpoint cannot overwrite arbitrary config")


def test_restart_mid_turn_still_delivers_text():
    """The exact failure seen live: only '✅ · 4м 47с' arrived, no content."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restart.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write("")
    _append(path, text="Текст, написанный до подключения демона.")

    d = make_daemon("s.json", away=False)          # fresh daemon: nothing tracked
    d.cfg["live_messages"] = True
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.handle_event(evt("Stop", transcript_path=path))

    body = "\n".join(m["text"] for m in d.bot.sent)
    assert "Текст, написанный до подключения демона." in body, d.bot.sent
    os.remove(path)
    print("PASS a daemon started mid-turn still delivers the turn's text")


def test_session_start_drains_queue():
    d = make_daemon("s.json", away=False)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key] = TOPIC
    d.queue[key] = ["задача из телеги"]
    out = d.handle_event(evt("SessionStart"))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "задача из телеги" in ctx, out
    print("PASS SessionStart hands over the queue")


def test_register_cannot_be_stolen():
    """A bot's username is public; /register from a stranger's chat must not
    redirect everything this bridge reports."""
    d = make_daemon("s.json", away=False)
    d.cfg["chat_id"] = -100
    d.on_command("/register", 777, None, {"chat": {"id": 777}})
    assert d.cfg["chat_id"] == -100, "an outsider rebound the bridge"
    assert d.bot.sent == [], "must not even confirm the bot exists"
    print("PASS /register cannot be hijacked from another chat")


def test_register_binds_an_unbound_bridge():
    d = make_daemon("s.json", away=False)
    d.cfg["chat_id"] = None
    d.refresh_status = lambda: None
    d.ensure_service_topics = lambda: None
    d.on_command("/register", 555, None, {"chat": {"id": 555}})
    assert d.cfg["chat_id"] == 555, d.cfg["chat_id"]
    print("PASS /register binds a bridge that has no group yet")


def test_unregister_forgets_the_topics():
    """Topic ids belong to the group that owned them; carrying them into the
    next group would post into whatever thread happens to share the number."""
    d = make_daemon("s.json", away=False)
    d.state["topics"][cfgmod.normalize(PROJECT)] = TOPIC
    d.state["status_message_id"] = 7
    d.on_command("/unregister", -100, None, {"chat": {"id": -100}})
    assert d.cfg["chat_id"] is None, d.cfg["chat_id"]
    assert d.state["topics"] == {}, d.state["topics"]
    assert d.state["status_message_id"] is None
    print("PASS /unregister releases the group and its topic ids")


def test_spawn_is_off_by_default():
    d = make_daemon("s.json", away=True)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key] = TOPIC
    calls = []
    d.run_spawn = lambda *a: calls.append(a)
    d.deliver_text("собери релиз", TOPIC,
                   {"chat": {"id": -100}, "message_thread_id": TOPIC})
    assert calls == [], "spawn must never run without being switched on"
    assert d.queue[key] == ["собери релиз"], d.queue
    print("PASS a task is queued, not spawned, while spawn is off")


def test_spawn_runs_when_no_session_is_listening():
    d = make_daemon("s.json", away=True)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key] = TOPIC
    d.cfg["spawn"] = {"enabled": True, "permission_mode": "acceptEdits",
                      "command": "claude-stub", "timeout_seconds": 60}
    started = []
    d.spawn_cwd = lambda k: "C:/Work/BridgeProject"
    d.run_spawn = lambda k, command, cwd, task: started.append((command, cwd, task))
    d.deliver_text("собери релиз", TOPIC,
                   {"chat": {"id": -100}, "message_thread_id": TOPIC})
    assert wait_for(lambda: started), "no agent was started"
    command, cwd, task = started[0]
    assert command[:3] == ["claude-stub", "-p", "собери релиз"], command
    assert "--permission-mode" in command and "acceptEdits" in command, command
    assert cwd == "C:/Work/BridgeProject", cwd
    assert key not in d.queue, "a spawned task must not also sit in the queue"
    print("PASS a task with no session starts a headless agent")


def test_an_answer_outlives_a_session_that_died_waiting():
    """Four hours is long enough to close the editor. The reply was typed and
    acknowledged; if the hook is gone by the time the answer goes back down
    the socket, it must not vanish with the connection."""
    d = make_daemon("s.json", away=True)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key] = TOPIC
    d.cfg["spawn"] = {"enabled": False}
    d.sessions["s1"] = {"project": key, "name": "P", "alive": True}

    d.rescue_answer({"session_id": "s1", "cwd": PROJECT},
                    {"decision": "block", "reason": "почини сборку"})
    assert d.queue.get(key) == ["почини сборку"], d.queue
    assert d.sessions["s1"].get("parked") is True, "it heard nothing"
    assert any("почини сборку" in m["text"] for m in d.bot.sent),         "the chat was not told the answer had been kept"

    # With an agent available it is started instead of queued.
    d2 = make_daemon("s2.json", away=True)
    d2.state["topics"][key] = TOPIC
    d2.cfg["spawn"] = {"enabled": True}
    d2.spawn_cwd = lambda k: "C:/Work/BridgeProject"
    started = []
    d2.run_spawn = lambda *a: started.append(a)
    d2.rescue_answer({"session_id": "gone", "cwd": PROJECT},
                     {"decision": "block", "reason": "подними сервер"})
    assert started, "nothing took the rescued answer"
    assert not d2.queue.get(key)
    print("PASS an answer survives the session it was meant for")


def test_a_session_that_stopped_listening_lets_a_spawn_through():
    """The trap this was built to close: the wait ran out, so the session no
    longer hears the chat, but it is still an open window. Counted as live, it
    kept the task in a queue for a session that was never coming back — the
    task sat there until somebody happened to type in that window."""
    d = make_daemon("s.json", away=True)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key] = TOPIC
    d.cfg["spawn"] = {"enabled": True, "command": "claude-stub"}
    d.sessions["s1"] = {"project": key, "name": "TGbotClaude", "alive": True}
    started = []
    d.spawn_cwd = lambda k: "C:/Work/BridgeProject"
    d.run_spawn = lambda *a: started.append(a)

    d.park("s1")
    assert d.live_session(key) is False, "a parked session must not count"
    d.deliver_text("собери релиз", TOPIC,
                   {"chat": {"id": -100}, "message_thread_id": TOPIC})
    assert started, "nothing picked the task up"
    assert not d.queue.get(key), "it should have been taken, not queued"

    # Using that window again puts it back in charge of its own project.
    d.handle_event(evt("UserPromptSubmit", session_id="s1"))
    assert d.sessions["s1"].get("parked") is False
    assert d.live_session(key) is True
    print("PASS a session that stopped listening no longer blocks a spawn")


def test_a_run_out_wait_says_what_happens_next():
    d = make_daemon("s.json", away=True)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key] = TOPIC
    d.cfg["wait_seconds"] = 0.1
    d.cfg["spawn"] = {"enabled": True}
    d.spawn_cwd = lambda k: "C:/Work/BridgeProject"

    box, thread = run_async(d, evt("Stop", session_id="s1"))
    thread.join(5)
    assert d.sessions["s1"].get("parked") is True, "the session was not parked"
    said = " ".join(m["text"] for m in d.bot.sent)
    assert i18n.t("stop.parked") in said, said
    assert i18n.t("stop.timeout") not in said, "the wrong ending was reported"

    # With no agent to start, it promises the queue instead.
    d.cfg["spawn"] = {"enabled": False}
    assert d.can_spawn(key) is False
    print("PASS a wait that runs out says whether a task would start a session")


def test_an_old_config_gets_the_longer_wait():
    """An hour was never chosen by anyone — it was the old default, and it is
    what made sessions unreachable. Raise it, but never override a real choice."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legacy.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"bot_token": "x", "wait_seconds": 3300,
                   "hook_timeout_seconds": 3600}, f)
    cfg = cfgmod.load(path)
    assert cfg["wait_seconds"] == cfgmod.DEFAULTS["wait_seconds"]
    assert cfg["hook_timeout_seconds"] == cfgmod.DEFAULTS["hook_timeout_seconds"]

    with open(path, "w", encoding="utf-8") as f:
        json.dump({"bot_token": "x", "wait_seconds": 600}, f)
    assert cfgmod.load(path)["wait_seconds"] == 600, "a chosen value must stand"
    os.remove(path)
    print("PASS an hour-long wait from an older config is raised")


def test_spawn_stands_aside_for_a_live_session():
    """A live session gets the task through its own Stop hook — starting a
    second agent in the same tree would have two of them editing at once."""
    d = make_daemon("s.json", away=True)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key] = TOPIC
    d.cfg["spawn"] = {"enabled": True, "command": "claude-stub"}
    d.sessions["s1"] = {"project": key, "name": "TGbotClaude", "alive": True}
    started = []
    d.spawn_cwd = lambda k: "C:/Work/BridgeProject"
    d.run_spawn = lambda *a: started.append(a)
    d.deliver_text("собери релиз", TOPIC,
                   {"chat": {"id": -100}, "message_thread_id": TOPIC})
    assert started == [], "spawned an agent next to a live session"
    assert d.queue[key] == ["собери релиз"], d.queue
    print("PASS a live session keeps the task instead of spawning")


def test_spawn_refuses_a_project_without_a_directory():
    """Auto-discovered projects are keyed by their transcript folder, which is
    not a tree anything can be built in."""
    d = make_daemon("s.json", away=True)
    key = "c:/users/me/.claude/projects/d--work-thing"
    d.state["topics"][key] = TOPIC
    d.cfg["spawn"] = {"enabled": True, "command": "claude-stub"}
    started = []
    d.run_spawn = lambda *a: started.append(a)
    d.deliver_text("собери релиз", TOPIC,
                   {"chat": {"id": -100}, "message_thread_id": TOPIC})
    assert started == [], "spawned into a directory that is not the project"
    assert d.queue[key] == ["собери релиз"], d.queue
    print("PASS an auto-discovered project is queued, never spawned")


def test_spawn_runs_one_agent_per_project():
    d = make_daemon("s.json", away=True)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key] = TOPIC
    d.cfg["spawn"] = {"enabled": True, "command": "claude-stub"}
    d.spawn_cwd = lambda k: "C:/Work/BridgeProject"
    started = []
    d.run_spawn = lambda *a: started.append(a)     # never clears `spawning`
    message = {"chat": {"id": -100}, "message_thread_id": TOPIC}
    d.deliver_text("первая", TOPIC, message)
    d.deliver_text("вторая", TOPIC, message)
    assert wait_for(lambda: started), "no agent was started"
    assert len(started) == 1, started
    assert d.queue[key] == ["вторая"], d.queue
    print("PASS the second task waits in the queue, not in a second agent")


def test_spawn_ignores_an_unknown_permission_mode():
    """The CLI refuses to start on an unknown --permission-mode, so the
    historic "default" must mean "do not pass the flag"."""
    d = make_daemon("s.json", away=True)
    d.cfg["spawn"] = {"enabled": True, "permission_mode": "default",
                      "command": "claude-stub"}
    command = d.spawn_command("почини")
    assert "--permission-mode" not in command, command
    print("PASS an unknown permission mode is dropped, not passed on")


def test_service_topics_take_no_tasks():
    """The limits and status topics belong to no project, so text written
    there used to pile up in a queue nothing would ever drain."""
    d = make_daemon("s.json", away=True)
    d.state["topics"][d.USAGE_KEY] = 99
    d.deliver_text("а тут что", 99, {"chat": {"id": -100},
                                     "message_thread_id": 99})
    assert d.queue == {}, d.queue
    print("PASS a service topic does not collect tasks")


def test_spawn_does_not_inherit_the_session_that_started_the_daemon():
    """Seen live: an agent spawned into an empty sandbox described another
    project's folders as its own, because the daemon had been started from a
    hook and carried that session's environment into it."""
    d = make_daemon("s.json", away=False)
    original = dict(os.environ)
    os.environ.update({"CLAUDECODE": "1", "CLAUDE_SESSION_ID": "abc",
                       "CLAUDE_CODE_CHILD_SESSION": "1",
                       "ANTHROPIC_MODEL": "claude-opus-5[1m]",
                       "PATH": original.get("PATH", ""), "KEEP_ME": "yes"})
    try:
        env = d.spawn_env()
        assert "CLAUDECODE" not in env and "CLAUDE_SESSION_ID" not in env, env
        assert "CLAUDE_CODE_CHILD_SESSION" not in env, "child-session flag leaked"
        assert "ANTHROPIC_MODEL" not in env, "the parent's model pin leaked"
        assert env.get("KEEP_ME") == "yes", "unrelated variables must survive"
        assert env.get("PATH"), "PATH must survive or claude cannot be found"

        # Started normally (no session around), the user's own vars are theirs.
        del os.environ["CLAUDECODE"]
        plain = d.spawn_env()
        assert plain.get("ANTHROPIC_MODEL") == "claude-opus-5[1m]", plain
    finally:
        os.environ.clear()
        os.environ.update(original)
    print("PASS a spawned agent starts without the parent session's environment")


def test_spawn_reports_a_binary_that_will_not_start():
    """The launch failure has to reach the chat: the hooks report a session
    that started, and a spawn that never started has no hooks to speak for it."""
    d = make_daemon("s.json", away=True)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key] = TOPIC
    d.spawning = {key}
    d.run_spawn(key, ["claude-that-is-not-installed", "-p", "почини"],
                os.path.dirname(os.path.abspath(__file__)), "почини")
    body = "\n".join(m["text"] for m in d.bot.sent)
    assert "🛑" in body, d.bot.sent
    assert key not in d.spawning, "a failed spawn must not block the next one"
    assert not d.bot.sent[-1]["silent"], "a launch failure must notify"
    print("PASS a spawn that cannot start says so and unblocks the project")


def test_stats_stay_bounded():
    d = make_daemon("s.json", away=False)
    d.cfg["stats_retention_days"] = 3
    d.state["stats"] = {f"2026-01-{day:02d}": {"x": {"turns": 1}}
                        for day in range(1, 21)}
    d.bump_stat(cfgmod.normalize(PROJECT), "turns", 1)
    days = sorted(d.state["stats"])
    assert len(days) == 3, days                    # the window counts today in
    assert days[:2] == ["2026-01-19", "2026-01-20"], days
    assert days[-1] == time.strftime("%Y-%m-%d"), days
    print("PASS the daily tallies are pruned to the retention window")


def test_spawn_toggle_reads_as_off_by_default():
    """get_setting used to answer True for any missing nested key — on
    spawn.enabled that would have advertised remote execution as on."""
    d = make_daemon("s.json", away=False)
    d.cfg["spawn"] = {}
    assert d.get_setting("spawn.enabled") is False
    assert d.get_setting("watchdog.enabled") is True
    print("PASS a missing nested toggle reads as its shipped default")


# Trimmed from a live answer of /api/oauth/usage, keys and shapes intact.
USAGE_PAYLOAD = {
    "five_hour": {"utilization": 95.0,
                  "resets_at": "2026-07-30T18:20:00.832597+00:00",
                  "limit_dollars": None},
    "seven_day": {"utilization": 53.0,
                  "resets_at": "2026-08-03T13:00:00.832616+00:00"},
    "seven_day_opus": None,
    "limits": [
        {"kind": "session", "group": "session", "percent": 95,
         "severity": "critical", "resets_at": "2026-07-30T18:20:00.832597+00:00",
         "scope": None, "is_active": True},
        {"kind": "weekly_all", "group": "weekly", "percent": 53,
         "severity": "normal", "resets_at": "2026-08-03T13:00:00.832616+00:00",
         "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 18,
         "severity": "normal", "resets_at": "2026-08-03T12:59:59.832894+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"},
                   "surface": None}, "is_active": False},
    ],
}


def test_usage_payload_maps_onto_the_status_line_shape():
    limits = usage.windows_from(USAGE_PAYLOAD)
    assert limits["five_hour"]["used_percentage"] == 95.0, limits
    assert limits["seven_day"]["used_percentage"] == 53.0, limits
    # ISO-8601 with an offset must become the epoch seconds usage.when() eats
    assert isinstance(limits["five_hour"]["resets_at"], float), limits
    assert usage.when(limits["five_hour"]["resets_at"]), "reset time unreadable"
    print("PASS the usage answer maps onto the status line's own shape")


def test_usage_payload_carries_the_fable_pool():
    """The per-model window only ever appears as a scoped weekly entry, and
    the widget reads it from a nested section named after the pool."""
    limits = usage.windows_from(USAGE_PAYLOAD)
    assert limits["fable"]["seven_day"]["used_percentage"] == 18.0, limits
    assert "five_hour" not in limits["fable"], "Fable has no 5-hour window"
    print("PASS the scoped weekly window lands in the Fable pool")


def test_usage_payload_without_limits_is_not_stored():
    assert usage.windows_from({"limits": [], "five_hour": None}) == {}
    print("PASS an empty usage answer maps to nothing")


def test_expired_credentials_are_refused():
    """An expired token would only earn a 401; the CLI rewrites the file when
    it refreshes, so the next read picks the new one up."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "test-credentials.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"claudeAiOauth": {"accessToken": "secret",
                                     "expiresAt": (time.time() - 60) * 1000}}, f)
    assert usage.access_token(path) is None
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"claudeAiOauth": {"accessToken": "secret",
                                     "expiresAt": (time.time() + 600) * 1000}}, f)
    assert usage.access_token(path) == "secret"
    os.remove(path)
    print("PASS an expired CLI token is not used")


def test_fresh_status_line_keeps_the_poll_quiet():
    """The status line stays the primary source: a cache it just wrote must
    stop the daemon from spending a request."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "test-usage.json")
    usage.store({"captured_at": time.time(), "rate_limits": {"five_hour": {}},
                 "has_limits": True}, path)
    assert usage.status_line_fresh(45, path) is True
    usage.store({"captured_at": time.time() - 600, "rate_limits": {},
                 "has_limits": True}, path)
    assert usage.status_line_fresh(45, path) is False
    os.remove(path)
    print("PASS a freshly written cache suppresses the poll")


def test_the_polls_own_output_does_not_suppress_the_next_poll():
    """Measured live: counting our own record as a fresh status line stretched
    the refresh from the configured 30s out to the 45s staleness window."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "test-usage.json")
    usage.store({"captured_at": time.time(), "rate_limits": {"five_hour": {}},
                 "has_limits": True, "source": "oauth"}, path)
    assert usage.status_line_fresh(45, path) is False
    os.remove(path)
    print("PASS the poll does not mistake its own output for the status line")


def test_usage_poll_sleeps_on_every_path():
    """A `continue` that skipped the sleep turned this thread into a busy
    loop that pinned a core for as long as the cache stayed fresh."""
    d = make_daemon("s.json", away=False)
    d.cfg["usage_poll"] = {"enabled": True, "interval_seconds": 30}
    naps, calls = [], []
    original_sleep, original_fresh = daemon_module.time.sleep, usage.status_line_fresh
    daemon_module.time.sleep = lambda s: (naps.append(s),
                                          calls.append(1),
                                          (_ for _ in ()).throw(StopIteration)
                                          if len(calls) > 3 else None)[-1]
    usage.status_line_fresh = lambda *a, **k: True      # nothing to fetch
    try:
        d.usage_poll_forever()
    except StopIteration:
        pass
    finally:
        daemon_module.time.sleep = original_sleep
        usage.status_line_fresh = original_fresh
    assert naps and all(n >= 10 for n in naps), naps
    print("PASS the usage poll sleeps on every path, fresh cache included")


def test_report_includes_a_scoped_pool():
    """A spent Fable window used to be invisible outside the widget: the
    report only ever walked the two shared windows."""
    body = usage.report({"rate_limits": {
        "five_hour": {"used_percentage": 12.0},
        "seven_day": {"used_percentage": 20.0},
        "fable": {"seven_day": {"used_percentage": 95.0}}}})
    assert "95%" in body and "Fable" in body, body
    assert body.index("20%") < body.index("95%"), "shared windows come first"
    print("PASS the limits report carries the per-model pool too")


def test_report_without_a_scoped_pool_is_unchanged():
    body = usage.report({"rate_limits": {"five_hour": {"used_percentage": 12.0}}})
    assert body.count("\n") == 0 and "12%" in body, body
    print("PASS a report with no scoped pool stays a plain two-window report")


def test_version_comparison_never_downgrades():
    from claudetg import version
    assert version.is_newer("1.0.1", "1.0.0")
    assert version.is_newer("v1.2.0", "1.1.9"), "a leading v is common in tags"
    assert version.is_newer("1.10.0", "1.9.0"), "compared as numbers, not text"
    assert not version.is_newer("1.0.0", "1.0.0")
    assert not version.is_newer("0.9.9", "1.0.0")
    # A tag nobody can parse must never look newer than what is installed.
    assert not version.is_newer("nightly", "1.0.0")
    assert not version.is_newer("", "1.0.0")
    print("PASS version comparison orders releases and rejects junk tags")


def test_update_refuses_what_it_cannot_verify():
    """An unverified download is reported, never installed: without a code
    signature the checksum is the only thing standing behind that binary."""
    from claudetg import updater
    release = {"version": "1.1.0", "name": "Setup.exe", "sha256": "",
               "url": "https://github.com/x/y/releases/download/1.1.0/Setup.exe"}
    try:
        updater.download(release, into=os.path.dirname(os.path.abspath(__file__)))
        raise AssertionError("installed a file with no checksum")
    except updater.UpdateError as e:
        assert "SHA-256" in str(e), e
    print("PASS an update with no published checksum is refused")


def test_update_refuses_a_foreign_download_host():
    from claudetg import updater
    release = {"version": "1.1.0", "name": "Setup.exe", "sha256": "a" * 64,
               "url": "https://evil.example.com/Setup.exe"}
    try:
        updater.download(release)
        raise AssertionError("downloaded from an arbitrary host")
    except updater.UpdateError as e:
        assert "refusing" in str(e), e
    print("PASS an update hosted anywhere but GitHub is refused")


def test_checksum_is_read_from_the_release_notes():
    from claudetg import updater
    digest = "b" * 64
    both = updater.checksum_for("ClaudeTelegram-Setup-1.1.0.exe",
                                "%s  ClaudeTelegram-Setup-1.1.0.exe" % digest)
    assert both == digest, both
    labelled = updater.checksum_for("other.exe", "SHA256: %s" % digest.upper())
    assert labelled == digest, "the label form must be accepted, case-folded"
    assert updater.checksum_for("x.exe", "no checksum here") == ""
    print("PASS the checksum is picked out of the release notes")


def test_update_waits_for_the_bridge_to_fall_quiet():
    """Updating restarts the daemon, and a hook blocked on a question would go
    down with it — taking the answer the user was about to give."""
    d = make_daemon("s.json", away=True)
    assert d.idle_enough_to_update() is True

    d.sessions["s1"] = {"project": "p", "alive": True}
    assert d.idle_enough_to_update() is False, "a live session must hold it off"

    d.sessions.clear()
    waiter = daemon_module.Waiter("ask", "s1", "p")
    d.waiters[waiter.id] = waiter
    assert d.idle_enough_to_update() is False, "a blocked hook must hold it off"

    d.waiters.clear()
    d.spawning.add("p")
    assert d.idle_enough_to_update() is False, "a running agent must hold it off"

    d.spawning.clear()
    assert d.idle_enough_to_update() is True
    print("PASS an update waits for no sessions, no waiters, no agents")


def test_a_failed_update_is_not_retried_forever():
    d = make_daemon("s.json", away=False)
    release = {"version": "1.1.0", "name": "Setup.exe", "sha256": "",
               "url": "https://github.com/x/y/releases/download/1.1.0/Setup.exe"}
    d.apply_update(release)
    assert d.state.get("update_failed") == "1.1.0", d.state
    launched = []
    original = daemon_module.updater.launch
    daemon_module.updater.launch = lambda p: launched.append(p)
    try:
        d.apply_update(release)         # same broken release, second time
    finally:
        daemon_module.updater.launch = original
    assert not launched, "a release that failed verification was retried"
    print("PASS a release that fails verification is not retried in a loop")


def test_a_rate_limited_poll_backs_off_for_as_long_as_asked():
    """Seen live: polling every 30s earned a stream of 429s. Asking again on
    the usual cadence just earns another refusal."""
    import urllib.error
    from claudetg import usage as usage_mod

    class Refusing:
        headers = {"Retry-After": "120"}
        code = 429
        def read(self): return b""

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 429, "Too Many Requests",
                                     {"Retry-After": "120"}, None)

    original = usage_mod.urllib.request.urlopen
    usage_mod.urllib.request.urlopen = boom
    try:
        usage_mod.fetch_live(token="x")
        raise AssertionError("a 429 was swallowed")
    except usage_mod.UsageError as e:
        assert e.retry_after == 120, e.retry_after
    finally:
        usage_mod.urllib.request.urlopen = original

    def boom_no_header(*a, **k):
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
    usage_mod.urllib.request.urlopen = boom_no_header
    try:
        usage_mod.fetch_live(token="x")
    except usage_mod.UsageError as e:
        assert e.retry_after >= 300, "no Retry-After means back off hard"
    finally:
        usage_mod.urllib.request.urlopen = original

    # any other failure must not silently pause the poll for minutes
    def other(*a, **k):
        raise urllib.error.URLError("no network")
    usage_mod.urllib.request.urlopen = other
    try:
        usage_mod.fetch_live(token="x")
    except usage_mod.UsageError as e:
        assert e.retry_after == 0, e.retry_after
    finally:
        usage_mod.urllib.request.urlopen = original
    print("PASS a rate-limited poll waits as long as the server asked")


def test_every_session_of_a_project_shares_one_topic():
    """Numbered threads per session were tried and taken back out: a session
    that finishes its task stops listening, so its own thread became a dead
    end nobody could talk to, and the group filled up with them."""
    d = make_daemon("s.json", away=False)
    key = cfgmod.normalize(PROJECT)
    d.sessions["a"] = {"project": key, "name": "P", "alive": True}
    d.sessions["b"] = {"project": key, "name": "P", "alive": True}
    d.state["topics"][key] = TOPIC          # as any earlier version left it

    assert d.topic_for(key, "P") == TOPIC
    assert d.topic_for(key, "P") == TOPIC, "a second window creates nothing"
    made = [c for c in d.bot.callbacks if c[0] == "create_topic"]
    assert made == [], "no topic should have been created"
    assert [k for k in d.state["topics"] if "#" in k] == [], d.state["topics"]
    print("PASS every window on a project talks in the project's own topic")


def test_a_topic_is_created_once_and_kept():
    d = make_daemon("s.json", away=False)
    key = "d:/work/fresh"
    d.state["topics"].pop(key, None)
    first = d.topic_for(key, "Fresh")
    second = d.topic_for(key, "Fresh")
    assert first == second, "the topic must be reused, not recreated"
    titles = [c for c in d.bot.callbacks if c[0] == "create_topic"]
    assert len(titles) == 1, titles
    assert titles[0][2] == "Fresh", "the title carries no number any more"
    print("PASS a project topic is created once, named after the project")


def test_an_answer_in_an_old_numbered_topic_still_reaches_the_session():
    """Groups that ran the numbered-thread version still have those topics.
    Nothing writes to them now, but a reply typed into one out of habit must
    still wake the session rather than queue a task in front of it."""
    d = make_daemon("s.json", away=True)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key] = 72
    d.state["topics"][key + "#2"] = 446      # left over from that version

    waiter = daemon_module.Waiter("stop", "a", key)
    d.waiters[waiter.id] = waiter
    d.by_topic[72] = waiter.id

    d.deliver_text("продовжуй", 446,
                   {"chat": {"id": -100}, "message_thread_id": 446})
    assert waiter.result == {"action": "continue", "text": "продовжуй"},         "the session was left asleep"
    assert not d.queue.get(key), "it should have woken it, not queued"
    print("PASS a reply in a leftover thread still wakes the session")


def test_a_window_closed_without_goodbye_is_forgotten():
    """A window killed without a SessionEnd left a session that looked alive
    forever, which convinced spawn that somebody was listening."""
    d = make_daemon("s.json", away=False)
    key = cfgmod.normalize(PROJECT)
    d.sessions["ghost"] = {"project": key, "name": "P", "alive": True,
                           "last_seen": time.time() - d.SESSION_TTL - 60}
    d.sessions["real"] = {"project": key, "name": "P", "alive": True,
                          "last_seen": time.time()}
    assert d.session_alive(d.sessions["real"]) is True
    assert d.session_alive(d.sessions["ghost"]) is False

    d.forget_stale_sessions()
    assert "ghost" not in d.sessions, "the stale session was kept"
    assert "real" in d.sessions, "a live session must survive the sweep"

    d.sessions.pop("real")
    assert d.live_session(key) is False, "nothing should block a spawn now"
    print("PASS a session with no events for hours is forgotten")


def test_a_task_in_any_thread_queues_for_the_project():
    """The queue stays per project: a task typed into #2 is still a task for
    that project, and should go to whichever session picks it up first."""
    d = make_daemon("s.json", away=True)
    key = cfgmod.normalize(PROJECT)
    d.state["topics"][key + "#2"] = 22
    d.deliver_text("почини сборку", 22,
                   {"chat": {"id": -100}, "message_thread_id": 22})
    assert d.queue.get(key) == ["почини сборку"], d.queue
    print("PASS a task typed in a numbered thread queues under the project")


def test_a_refused_poll_slows_the_whole_cadence_down():
    """Seen live: the poll flapped fail/ok every other minute for hours. The
    server answered `Retry-After: 60` to a poll it refused for running every
    60 seconds, so obeying it exactly walked straight back into the refusal."""
    D = Daemon
    interval = 60
    floor = 0
    # Each refusal walks the poll further out instead of repeating the same ask.
    floor = D.widen_poll(floor, interval, 60)
    assert floor == 120, floor
    floor = D.widen_poll(floor, interval, 60)
    assert floor == 240, floor
    # A server asking for longer than our own backoff still wins.
    assert D.widen_poll(120, interval, 600) == 600
    # And it never runs away: the ceiling holds.
    assert D.widen_poll(D.USAGE_FLOOR_MAX, interval, 60) == D.USAGE_FLOOR_MAX

    # Once the refusals stop, the poll speeds back up to the configured rate.
    floor = 240
    for _ in range(5):
        floor = D.calm_poll(floor, interval)
    assert floor == interval, floor
    print("PASS a refused poll widens the cadence and recovers it")


def test_nothing_ever_relaunches_itself_as_the_daemon():
    """Seen live: a hook client that could not reach the daemon started the
    daemon with `sys.executable -m claudetg.daemon`. Frozen, sys.executable is
    the hook client, so it started another hook client, which started another
    one — a fresh process every two seconds. The widget had the same line and
    kept re-launching the widget, so the daemon never came up at all."""
    import hookc

    frozen = getattr(sys, "frozen", False)
    try:
        sys.frozen = True
        for name, command in (("paths", paths.daemon_command()),
                              ("hookc", hookc.daemon_command())):
            # Missing executable is a refusal, not a guess.
            assert command is None or command[0].endswith("claudetg-daemon.exe"), (
                f"{name} would relaunch {command}")
            assert command is None or "-m" not in command, name
    finally:
        if frozen:
            sys.frozen = frozen
        else:
            del sys.frozen

    # From source there is no daemon executable, so the module is right.
    assert paths.daemon_command() == [sys.executable, "-m", "claudetg.daemon"]
    assert hookc.daemon_command() == [sys.executable, "-m", "claudetg.daemon"]
    print("PASS neither client can relaunch itself as the daemon")


def test_a_window_that_already_reset_is_not_shown_as_full():
    """Seen live: the poll could not get through for a day, and the cache
    still said 100% of the week — for a week that had ended the afternoon
    before. Showing that is worse than showing nothing: the limit was back."""
    now = time.time()
    data = {"has_limits": True, "captured_at": now - 26 * 3600, "rate_limits": {
        "five_hour": {"used_percentage": 0.0, "resets_at": None},
        "seven_day": {"used_percentage": 100.0, "resets_at": now - 3600},
        "fable": {"seven_day": {"used_percentage": 33.0, "resets_at": now + 86400}},
    }}
    clean = usage.expire_spent_windows(data, now=now)["rate_limits"]
    assert clean["seven_day"]["used_percentage"] is None, "a reset week was kept"
    # No reset time at all: the window's own length says when it went stale.
    assert clean["five_hour"]["used_percentage"] is None, "a day-old 5h reading"
    assert clean["fable"]["seven_day"]["used_percentage"] == 33.0,         "a window that has not reset must survive"

    fresh = dict(data, captured_at=now - 600)
    fresh["rate_limits"] = dict(data["rate_limits"],
                                five_hour={"used_percentage": 12.0, "resets_at": None})
    kept = usage.expire_spent_windows(fresh, now=now)["rate_limits"]
    assert kept["five_hour"]["used_percentage"] == 12.0, "a recent reading was dropped"
    print("PASS a window that already reset is reported as unknown")


def test_a_refusal_outlives_the_process():
    """A reboot used to wipe the server's cooldown and ask again at once,
    which is how one refusal turned into a longer one."""
    d = make_daemon("s.json", away=False)
    assert d.usage_hold_left() == 0
    d.hold_usage(1633)
    left = d.usage_hold_left()
    assert 1600 < left <= 1633, left

    # A fresh daemon reading the same state must still wait.
    again = make_daemon("s.json", away=False)
    again.state["usage_hold_until"] = d.state["usage_hold_until"]
    assert again.usage_hold_left() > 1600

    # And the refresh button spends none of it.
    asked = []
    usage_fetch = usage.fetch_live
    usage.fetch_live = lambda **kw: asked.append(kw) or None
    try:
        answer = again.poll_usage_now()
    finally:
        usage.fetch_live = usage_fetch
    assert asked == [], "the button asked anyway and would earn a longer ban"
    assert answer["ok"] is False and answer["wait"] > 1600, answer
    print("PASS a rate-limit cooldown survives a restart and the button")


def test_the_refresh_button_stores_what_it_reads():
    d = make_daemon("s.json", away=False)
    d.state.pop("usage_hold_until", None)
    record = {"captured_at": time.time(), "has_limits": True, "source": "oauth",
              "rate_limits": {"five_hour": {"used_percentage": 7.0,
                                            "resets_at": time.time() + 3600}}}
    stored, fetch = [], usage.fetch_live
    usage.fetch_live = lambda **kw: record
    store = usage.store
    usage.store = lambda r, path=None: stored.append(r)
    try:
        answer = d.poll_usage_now()
    finally:
        usage.fetch_live, usage.store = fetch, store
    assert answer["ok"] is True and stored == [record], answer
    print("PASS the refresh button stores the reading it got")


def test_the_pace_a_refusal_taught_is_not_forgotten():
    """517 refusals in one day, then 244, then a half-hour penalty. The poll
    did learn to slow down — and threw the lesson away on every restart, of
    which there can be a dozen in a day."""
    d = make_daemon("s.json", away=False)
    d.hold_usage(600, floor=240)
    assert d.state["usage_floor"] == 240, d.state
    assert d.state["usage_hold_until"] > time.time()

    # A daemon starting up reads the pace back rather than the fast default.
    again = make_daemon("s.json", away=False)
    again.state.update({"usage_floor": d.state["usage_floor"]})
    assert (again.state.get("usage_floor") or 0) == 240

    # Sustained success walks it back down to the configured interval.
    floor = 240
    for _ in range(5):
        floor = Daemon.calm_poll(floor, 180)
    again.remember_floor(floor)
    assert again.state["usage_floor"] == 180, again.state

    # And the configured interval itself is no longer the one that earned them.
    assert cfgmod.DEFAULTS["usage_poll"]["interval_seconds"] >= 120
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legacy2.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"bot_token": "x", "usage_poll": {"enabled": True,
                                                    "interval_seconds": 60}}, f)
    assert cfgmod.load(path)["usage_poll"]["interval_seconds"] ==         cfgmod.DEFAULTS["usage_poll"]["interval_seconds"], "an old interval stood"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"bot_token": "x", "usage_poll": {"interval_seconds": 45}}, f)
    assert cfgmod.load(path)["usage_poll"]["interval_seconds"] == 45,         "a chosen interval must stand"
    os.remove(path)
    print("PASS the pace a refusal taught survives a restart")


def test_the_suite_never_writes_the_real_config():
    """A guard, not a feature test. The daemon persists config on /register,
    /projects and every settings change; if those paths ever point at the
    installed config.json again, a test run wipes the live bot token."""
    real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config.json")
    assert os.path.abspath(cfgmod.PATH) != os.path.abspath(real), cfgmod.PATH
    assert "tests" in os.path.abspath(cfgmod.PATH), cfgmod.PATH
    assert "tests" in os.path.abspath(cfgmod.STATE_PATH), cfgmod.STATE_PATH

    # Prove it by writing: the redirection used to be defeated by `path=PATH`
    # default arguments, which bind at import time and ignore the reassignment.
    before = open(real, encoding="utf-8").read() if os.path.exists(real) else None
    d = make_daemon("s.json", away=False)
    d.apply_settings({"git_summary": False})
    d.select_projects([cfgmod.normalize(PROJECT)])
    d.on_command("/register", 4242, None, {"chat": {"id": 4242}})
    after = open(real, encoding="utf-8").read() if os.path.exists(real) else None
    assert before == after, "a test run rewrote the installed config.json"
    print("PASS the suite writes its config and state inside tests/")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print("\nfailures:", failures)
    sys.exit(1 if failures else 0)
