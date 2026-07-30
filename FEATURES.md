# What the bridge does

**English** · [Українська](FEATURES.uk.md) · [Русский](FEATURES.ru.md)

Every switch below lives in the widget, under **Features…**. You never need to
edit `config.json` by hand; changes apply instantly, with no restart.

---

## Presence mode — everything hangs off this

The checkbox in the widget (or `/away` and `/here` in the bot).

| Checkbox | What happens |
|---|---|
| **checked** — you are at the PC | everything reaches Telegram **silently**, nothing blocks, questions stay in VSCode |
| **unchecked** — you are away | hooks **block** the session and wait up to an hour for your answer in Telegram |

When you come back and check the box again, **every pending wait is released
at once** — the question returns to VSCode instead of the session waiting on a
chat you have stopped reading. `/release` does the same thing by hand.

---

## Questions as buttons

When Claude asks a multiple-choice question, it arrives in the project's topic
as buttons. Multi-select works too: tick several, press «Done». You can also
answer in your own words by replying to the question message.

Away mode only. At the PC the question stays in the editor.

## Tasks from your phone

When Claude finishes a turn, the topic gets a message with **«▶️ Continue»**
and **«⏹ Enough»**. Any text you write into the topic becomes the next task —
the session carries on without you.

If you do not answer within the hour, the session simply stops. Anything you
write **later** is not lost: Telegram keeps unread messages for about a day,
the bridge queues them and hands them over on the next turn or the next
session start. `/queue` shows what is waiting.

Without `spawn` (below), a task can only reach a session that exists — it
waits in the queue until you open the project.

## Headless agent · `spawn`

**Off by default.** When a task lands in a project's topic and no session is
listening, the bridge starts one: `claude -p "<your task>"` in the project's
directory.

The spawned agent is an ordinary Claude Code session, so the hooks report it
back through the very same bridge — live text, the closing summary, the git
note. Nothing about it is special-cased.

```json
"spawn": {
  "enabled": false,
  "permission_mode": "acceptEdits",
  "model": null,
  "timeout_seconds": 7200
}
```

- `permission_mode` — one of the CLI's own: `acceptEdits`, `auto`,
  `bypassPermissions`, `manual`, `dontAsk`, `plan`. Anything else means the
  flag is not passed at all and Claude Code uses its default.
- `model` — `null` keeps whatever the CLI is configured to use.
- `timeout_seconds` — a backstop against an orphan. Away mode lets the agent's
  own Stop hook hold it for `wait_seconds` on top of the actual work, so keep
  this generous. `0` means no limit.

**Boundaries, on purpose:**

- One agent per project. A second task while one is running goes to the queue.
- Never next to a live session — that session gets the task through its own
  Stop hook instead, so two agents never edit the same tree at once.
- Only for projects picked from the list. An auto-discovered project is keyed
  by its transcript folder, which is not a tree anything can be built in;
  those tasks are queued.
- If `claude` is not on `PATH`, the topic says so instead of failing quietly.

**This is remote code execution on your machine, triggered by a chat
message.** A leaked bot token becomes the right to run an agent in your
projects. Turn it on deliberately.

---

## Live text stream · `live_messages`

Text arrives **as it is written**, not in one dump at the end of the turn.
Live blocks are marked `💬`; the turn closes with a short `✅ Project · 1m 47s`.

The stream always runs, including while you are at the PC — just silently.

Formatting survives: bold, inline code, fenced code blocks with highlighting.
Long answers are cut on paragraph and code-block boundaries, never mid-line.
Markdown tables become monospace with aligned columns — Telegram has no tables
of its own.

## Todo list · `log_when_present.todo`

The todo list arrives as **one message that is edited in place**: `✅` done
(struck through), `🔸` in progress, `▫️` queued, plus a `2/7` counter. No new
message per update.

## Turn summary · `log_when_present.stop`

The `✅ Project · 1m 47s` line at the end of a turn. If no live blocks went out
for any reason (the daemon was started mid-turn, say), the **full text** comes
here instead — content is never lost.

## Session open and close · `log_when_present.session`

`🟢 session opened` and `⚪ session closed`. Closing adds a **git summary**
(`git_summary`): the branch and what is left uncommitted. Silent in non-git
projects.

## Tool failures · `report_tool_failures`

`❌ Project · Bash` with the command and the error text. **Off by default** —
it is noisy during active work. Failures are counted for the digest either way.

---

## Rate limits · `usage_report`

After each turn, as its own message in the **«Claude limits»** topic: how full
the 5-hour and weekly windows are, and **when they reset**.

```
📊 Limits
`████░░░░░░` 43% — 5-hour window
   resets at 17:34 (in 2h 15m)
`████████░░` 78% — Weekly
   resets 01.08 at 15:20
```

The data comes from Claude Code's status line (the `rate_limits` field) — no
browser sessions, no credentials. The same numbers show at the bottom of the
VSCode window.

If your Claude Code build does not report that field, the message simply does
not arrive. Percentages are never invented.

### When the status line never runs · `usage_poll`

**Off by default.** Claude Code renders the status line only in its terminal
UI. Work through the VSCode extension and the cache is never written at all —
the meters stay on `—` forever, and no amount of polling the file helps.

Turning `usage_poll` on lets the daemon ask the same endpoint the CLI itself
uses (`/api/oauth/usage`), with the CLI's own OAuth token read from
`~/.claude/.credentials.json`:

```json
"usage_poll": {
  "enabled": false,
  "interval_seconds": 30,
  "stale_after_seconds": 45,
  "timeout_seconds": 10
}
```

The status line stays the primary source: if the cache was written less than
`stale_after_seconds` ago, the poll spends nothing. It only fills silence.

This is the one part of the bridge that touches a credential, which is why it
is opt-in. The token never leaves your machine except to Anthropic, the same
place the CLI sends it. The endpoint is undocumented — if it changes, the poll
starts failing and the daemon logs it twice before backing off; the status
line path keeps working regardless.

It also surfaces the **Fable pool**, which the status line has never been seen
to report: the answer carries a per-model weekly window, and the widget gives
it a row of its own. Fable draws on the shared 5-hour window and only has a
weekly limit of its own, so there is nothing else to show.

## Anthropic service status · `status_monitor`

Polls `status.claude.com` every 5 minutes and writes into the **«Claude
status»** topic whenever something changes: the overall indicator, a
component's state (`Claude API`, `Claude Code`, …), a new incident with its
text, or its resolution. Outages arrive **with sound even while you are at the
PC** — the whole point is not having to watch the page yourself.

The first poll is silent: it only records the starting state. `/claude_status`
checks on demand.

## A turn killed by an API error

If a turn dies on an API error or a limit, `🛑 API failure` arrives with the
text. You usually learn about it before the status page admits it.

## Stalled-turn watchdog · `watchdog`

If a turn runs longer than 20 minutes without finishing, one `🐌` notice
arrives. It does not repeat for the same turn. Useful in away mode: otherwise
work and a hang look identical.

## Daily digest · `daily_digest`

At 21:00, a table by project: turns, errors, total time. The tallies are kept
for `stats_retention_days` (14 by default) and older days are dropped, so
`state.json` stays bounded.

---

## The Telegram screen

Widget menu → **Telegram…**, and it opens by itself until the bridge is set
up. It holds the whole installation in one place:

- the bot token, checked against Telegram **before** it is saved — you see
  `Bot @yourbot is connected` or the reason it was refused, instead of a
  daemon that quietly fails to start;
- whether a group is bound and how many topics exist;
- whether the Claude Code hooks are installed, with a button that installs
  them — the one step that used to need a terminal;
- the five setup steps, and a link that opens @BotFather.

Pasting a different token resets the binding on purpose: another bot means
another chat, and the old topic ids point at threads it cannot see.

## Projects

The **«Projects…»** button or the `/projects` command: the projects Claude Code
already knows, freshest first. The checked ones get their own Telegram topic.

That list is the privacy boundary: **an unchecked project sends nothing at
all**.

- **Subdirectories** land in their project's topic automatically.
- **Sibling working directories** (test data, a game client) can be bound to a
  project: `python install.py --add-path "C:/path" --for ProjectName`.
- **`auto_discover`** bridges any new project by itself. Convenient, but then
  the contents of every session on the machine go to Telegram, including
  incidental ones.
- **`exclude_paths`** — never bridged, even with `auto_discover` on.

---

## Bot commands

| Command | Effect |
|---|---|
| `/help` | the command list |
| `/register` | bind this group (once) |
| `/unregister` | release the binding and forget the topic ids |
| `/away`, `/here` | presence mode |
| `/status` | live sessions and what waits for an answer |
| `/projects` | project list; tapping bridges and unbridges |
| `/queue` | accumulated tasks |
| `/claude_status` | Anthropic service status |
| `/release` | release every pending wait |

Binding is one-way on purpose. A bot's username is discoverable and its token
is not needed to write to it, so a second `/register` from a stranger's chat
would redirect everything this bridge reports. Once bound, the bridge ignores
`/register` from anywhere else; rebinding takes `/unregister` from the chat
that currently owns it.

---

## Boundaries worth knowing

- **A wait is capped at an hour.** Measured: the hook holds a session for the
  full hour and it finishes the turn normally afterwards. Beyond that,
  untested.
- **The bridge speaks at turn boundaries.** Inside a long turn there are no
  events other than the live text and todo updates. Silence in a topic during
  long work is normal.
- **A question's answer** is delivered through the tool-denial mechanism, so
  the transcript shows it as a denied tool carrying the text of your choice.
  It does not affect the work.
- **A task cannot be pushed into a session that does not exist** unless
  `spawn` is on — otherwise it waits in the queue.
