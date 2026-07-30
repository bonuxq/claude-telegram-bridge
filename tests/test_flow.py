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
from claudetg import i18n, render  # noqa: E402

# The assertions below are written against the Russian bundle; pin it so the
# suite does not depend on the OS language of whoever runs it.
i18n.set_language("ru")

# Keep test noise out of the production log, or a real diagnosis later will
# stumble over lines written by a test run.
daemon_module.LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "test-daemon.log")

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
                # Off by default so tests never depend on a real usage.json
                # lying around on disk.
                "usage_report": {"enabled": False},
                "projects": {cfgmod.normalize(PROJECT): {"name": "TGbotClaude"}}})
    cfgmod.STATE_PATH = tmp_state
    d = Daemon.__new__(Daemon)
    d.cfg = cfg
    d.bot = FakeBot()
    d.state = {"topics": {}, "away": away, "offset": None, "status_message_id": None}
    d.lock = threading.RLock()
    d.tail_lock = threading.Lock()
    d.sessions, d.waiters, d.by_topic, d.queue = {}, {}, {}, {}
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
