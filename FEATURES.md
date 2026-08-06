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

When Claude finishes a turn, the topic gets a message with **Continue**,
**Waiting** and **Enough**. Any text you write into the topic becomes the next
task - the session carries on without you.

**Waiting** lets the turn end without saying a word to the session: nothing it
did not ask for lands in its transcript. Use it when it set itself something to
come back to and the wait is the only thing in the way. With `wake_alarm` on,
this leaves it an alarm as well.

A message typed **while a turn is running** does not wait for that turn to
finish. It is handed over at the session's next batch of tool calls, the same
way typing into the editor mid-action reaches it. The acknowledgement says
which of three things is happening:

| Reply | What it means |
|---|---|
| handing this over at its next step | a turn is running; seconds away |
| the session is between turns | the window is open; it takes this the moment it moves |
| nothing is open here | no session at all; the task waits for one |

Anything written when nothing is listening is not lost: Telegram keeps unread
messages for about a day, the bridge queues them and hands them over on the
next turn or the next session start. `/queue` shows what is waiting.

Without `spawn` (below), a task can only reach a session that exists - it waits
in the queue until you open the project.

## Holding a finished turn · `stop_grace`

The hook that ends a turn is the only door into a session. Once it has
answered, nothing outside can start a turn there: flipping to away a minute
later reaches a session that no longer hears anything, and the task you type
can only wait for whatever you next type at the keyboard.

So a turn that ends while you are at the PC keeps its hook for a while. The
clock is not the point - the desk is:

* **the first touch of keyboard or mouse ends it at once**, so a turn ending
  while you work is not held at all;
* a turn ending after you have walked away is held while nobody comes back, up
  to `seconds` (an hour by default);
* flipping the switch during the hold turns it into the ordinary away wait,
  buttons and all - same session, in the editor, no agent anywhere;
* a press of the switch is itself a click, and it reaches this machine before
  it reaches Telegram and comes back, so a touch waits out that round trip
  before it counts as somebody sitting back down. A press in Telegram or in the
  widget is not "back at work".

The summary is sent before the hold rather than after, so nothing you are
waiting for waits on it. Coming back to the keyboard releases the wait and then
holds once more, so stepping away again a moment later still finds a door open.

The one case a hold cannot cover: touching the desk and then leaving without
flipping the switch. While the hook is held the editor cannot be typed into, so
a touch has to end it.

## Alarm · `wake_alarm`

**Off by default.** What it solves: nothing outside a session can start a turn
in it. A session whose wait has run out is not asleep - it is unreachable, and
stays that way until somebody types at the keyboard.

A session can be woken by its own work, though. A command started in the
background wakes it when it finishes, so a wait that nobody answers ends by
asking the session to sleep for the interval and stop there. The turn it wakes
into is a turn the chat can reach.

**The round it makes, while you are away:**

1. a turn ends; the topic gets the summary and the buttons;
2. nobody answers for the interval - the wait is cut to it, rather than sitting
   there for the four hours it otherwise would;
3. the topic says `Nobody answered - checking back in N min`, and the session is
   asked to start `sleep` in the background and end the turn;
4. it wakes when that finishes, and anything typed meanwhile is handed to it at
   its first batch of tool calls;
5. that turn ends, and the round begins again.

So while you are away the session is reachable at every check-in, as well as
the whole time any turn is running. Pressing **Waiting** leaves the same alarm
behind, so answering does not end the round either.

**What it costs.** One turn per interval - small, but not nothing, and it is
your rate limit. Fifteen minutes is four an hour. That is why it ships off.

**What it cannot do.** The alarm is set from inside a hook, so a turn has to be
ending for one to be left behind. Flip the switch while a session already sits
idle - with the hold gone - and there is nothing to arm it from: the round
starts at the first turn that ends while away, and sustains itself from there.

**Where to set it.** The widget's menu has *Alarm...*: minutes, Apply, and zero
for off. The card shows how long it is set for, and says plainly when it is not
running, which is whenever you are at the PC.

## A topic per chat · `session_topics`

**Off by default**, and one topic per project without it, however many windows
are open on that project.

With it on, each chat gets a numbered thread of its own: the first keeps the
project's topic, the second becomes "Project #2". The number belongs to the
chat's session id and is never handed back, which is the whole difference from
the version of this that was tried and removed - that one numbered by slot and
freed the number when the window closed, so continuing the same conversation
landed it in whichever thread happened to be free.

Close the editor, reopen the same chat, and it goes on in its thread. Start a
new chat and it gets a new number. A thread whose chat can be reopened is not a
dead end.

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

**On by default**, because for most installs it is the only source there is.
Claude Code renders the status line only in its terminal UI. Work through the VSCode extension and the cache is never written at all —
the meters stay on `—` forever, and no amount of polling the file helps.

It lets the daemon ask the same endpoint the CLI itself uses (`/api/oauth/usage`), with the CLI's own OAuth token read from
`~/.claude/.credentials.json`:

```json
"usage_poll": {
  "enabled": true,
  "interval_seconds": 60,
  "stale_after_seconds": 45,
  "timeout_seconds": 10
}
```

The status line stays the primary source: if the cache was written less than
`stale_after_seconds` ago, the poll spends nothing. It only fills silence.

This is the one part of the bridge that touches a credential, so it is worth
saying plainly: the token never leaves your machine except to Anthropic, the
same place the CLI sends it, and it only ever asks for your own usage. Turn it
off in Features if you would rather it did not. The endpoint is undocumented — if it changes, the poll
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

## Updates · `updates`

**On by default, for installed builds only.** The daemon asks GitHub for the
latest release every few hours and installs it the first moment the bridge is
idle — no live session, nothing waiting on an answer, no spawned agent. An
update restarts the daemon, and a hook blocked on your question would go down
with it, taking the answer you were about to give.

```json
"updates": { "enabled": true, "interval_hours": 6 }
```

The installer upgrades in place: same install, hooks stay wired, `config.json`
is kept. You get one line in the **«Claude status»** topic when a release is
found and another when it is installed.

**It refuses to install what it cannot verify.** Every release publishes the
SHA-256 of its setup file in the release notes; a download whose hash does not
match, or that carries no published hash at all, is reported and left alone.
Downloads are only accepted from GitHub's own hosts. Until the builds are
code-signed, that checksum is the only thing standing behind the binary.

Running from source, this does nothing at all — there the update is
`git pull`. The widget's Telegram screen says which of the two you are on.

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
