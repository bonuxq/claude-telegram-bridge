# Claude Code ↔ Telegram bridge

**English** · [Українська](README.uk.md) · [Русский](README.ru.md)

Remote control for live Claude Code sessions from Telegram: questions arrive
as buttons, the assistant's text streams in as it is written, and your reply
in the chat resumes the work. One installer, no dependencies — the whole
bridge is Python standard library.

![Desktop widget](docs/widget.png)

## What you get

- **A topic per project** in one Telegram supergroup: nothing gets mixed up,
  and an unchecked project sends nothing at all.
- **Questions as buttons.** When Claude asks a multiple-choice question away
  from the PC, it lands in the project's topic with inline buttons; free-text
  replies work too.
- **Live text stream**: each block of the assistant's answer is delivered as
  it appears, not as one dump at the end of the turn.
- **Task queue from your phone**: any text you write into a project topic
  becomes the session's next task; unread replies are queued for up to a day.
- **A headless agent on demand** (`spawn`, off by default): a task for a
  project with no live session starts `claude -p` in that project's directory
  and reports back through the same bridge.
- **Rate-limit meters** (5-hour and weekly windows) after each turn and in
  the desktop widget, sourced from the Claude Code status line — no
  credentials, no scraping. The status line only runs in Claude Code's
  terminal UI, so the daemon fetches the same numbers itself every 30 seconds
  using the Claude Code token — that is what fills the meters on most setups.
- **A desktop widget**: borderless always-on-top card with a presence toggle,
  usage meters grouped per vendor (**Claude**: session, week, Fable —
  **Codex**: week), a tray icon showing the meter of your choice, transparency
  control, click-through mode and auto-away on keyboard idle. The card turns
  red on its own when a window is nearly spent, and Telegram can be switched
  off entirely to leave a pure limits monitor.
- **Self-updating**: the installed build fetches new releases from GitHub and
  installs them the first moment no session is running or waiting. It refuses
  any download whose published SHA-256 it cannot confirm.
- **Watchdog, daily digest, git summaries, status.claude.com monitor** — all
  optional, all toggleable from the widget.
- **9 languages** — English, Українська, Русский, 한국어, 中文, Tiếng Việt,
  Ελληνικά, Português, Español. Auto-detected from the OS; switchable from
  the widget menu (Language) or `"language"` in `config.json`. Translations
  are plain `lang/messages_*.properties` files — adding a language is one
  file, no code.

## How it works

The VSCode extension offers no way to inject a message into a running
session, so the bridge rides Claude Code's official **hooks**:

```
VSCode (session) --hook--> hookc.py --HTTP--> claudetg.daemon <--getUpdates--> Telegram
```

One resident daemon owns the bot (Telegram allows a single `getUpdates`
consumer per token), serves every session on the machine, and tails the
transcripts for the live stream. Hooks are a thin stdlib client that exits
instantly for projects you did not bridge.

**Presence is the core switch.** At the PC (checked): everything logs to
Telegram silently by default — or not at all, it is filterable per event
type. Away (unchecked): hooks block and wait up to an hour for your answer
in the chat; coming back releases every pending wait instantly.

## Requirements

- Windows
- Claude Code
- A Telegram bot token from [@BotFather](https://t.me/BotFather) — the setup
  screen walks you through getting one

Nothing else. The installer carries its own runtime; Python is only needed if
you run from source.

## Install

1. Download **`ClaudeTelegram-Setup.exe`** from the
   [releases](https://github.com/bonuxq/claude-telegram-bridge/releases) and
   run it. No administrator rights: it installs into your own profile and
   wires the Claude Code hooks itself.
2. The widget opens a setup screen. Paste the bot token — it is checked
   against Telegram before it is saved — then create a supergroup with
   **Topics** enabled, add the bot as an admin, and send `/register` in it.
3. Pick your projects: widget menu → **Projects…**, or `/projects` in the
   chat. Unchecked projects send nothing.

Restart any VSCode windows that were open during the install so they pick up
the hooks.

Full walkthrough, troubleshooting and the from-source route:
[INSTALL.md](INSTALL.md).

## Bot commands

| Command | Effect |
|---|---|
| `/help` | the command list |
| `/register` | bind this group (once) |
| `/unregister` | release the binding and forget the topic ids |
| `/away`, `/here` | presence mode |
| `/status` | live sessions and what waits for an answer |
| `/projects` | toggle bridged projects |
| `/queue` | pending queued tasks |
| `/claude_status` | Anthropic service status |
| `/release` | release all pending waits back to VSCode |

Binding is one-way: once bound, `/register` from any other chat is ignored, so
finding the bot is not enough to redirect its reports.

## The widget

A frameless dark card, always on top. Drag anywhere to move, right-click
(or the tray icon, or the gear) for the menu.

- **Presence capsule** — the big button; green at the PC, amber away, red
  when the daemon is down (a click brings it up).
- **Usage meters, one section per vendor** — **Claude** holds the shared
  5-hour window, the shared weekly one and Fable's own weekly window;
  **Codex** holds its weekly window. Both sections say "Week" and mean
  different weeks, which is what the headings are for. Reset times live in
  the tooltips. Click a heading to fold that vendor away; the **Fable** tab
  in the Claude heading folds just that row.
- **A section only exists while its vendor does** — no `~/.claude`, no Claude
  rows and no Fable tab; no Codex, no Codex row; neither, and the whole block
  goes rather than leaving empty meters behind.
- **Codex limits** are asked of the usage endpoint Codex's own CLI reads
  for `/status` (`chatgpt.com/backend-api/wham/usage`), once a minute, with
  the token the CLI keeps in `~/.codex/auth.json` — the same way `usage_poll`
  works for Claude, and off with `codex_poll.enabled` in Features. This is
  the only source that knows about a week spent from a phone or another
  machine. The logs Codex writes anyway (`~/.codex/sessions/**/rollout-*.jsonl`
  and `archived_sessions/`, the `token_count` events) are the fallback, and
  whichever reading is fresher wins. Codex reports one pool per model plus
  the plan's own; the row is the plan's weekly window, found by its length
  rather than by which slot it arrived in. A window that reset since the
  last reading shows 0%: the week it described has ended and nothing has
  been spent in the new one, or there would be a newer reading.
- **Telegram screen** — the master switch, the token field, what Telegram
  makes of it, whether the group is bound and whether the hooks are
  installed, plus the setup steps. Opens by itself until the bridge is
  configured.
- **Telegram can be switched off entirely** — first row of the Telegram
  screen, and the first toggle in Features (`telegram.enabled`). Off, the
  bridge sends nothing and listens for nothing, away collapses back to being
  at the PC (nobody could answer a blocked hook anyway), and the card drops
  its whole presence half: the capsule, the alarm row, and the menu entries
  that only mean something with a chat on the other end. What is left is a
  limits monitor with a gear on it. The token stays saved. A fresh install
  starts off; an install that already has a token and a group keeps working,
  because a missing switch means "on if it is configured".
- **Menu** — Telegram, Projects, Features (every daemon toggle, applied
  instantly), transparency presets, scale, click-through, auto-away, reset
  the widget, hide-to-tray. The gear's tooltip says which version is running.
- **Click-through** — the card stops catching the mouse, so it can sit over
  your editor without being in the way. The presence capsule and the gear
  stay live: each gets a solid stand-in window, with a matching hole cut in
  the card so nothing composites twice.
- **Alarm colours** — the card goes dim red past 90% of the 5-hour window and
  full red past 90% of either weekly one, and the capsule follows the card.
  The session meter has a finer ramp: yellow at 60%, orange through 80%, red
  at 90%.
- **Reset the widget's position** — a card parked on a monitor that is no
  longer plugged in comes back on its own, at startup and while running.
  The menu entry is the manual way out, and it also puts transparency and
  click-through back to their defaults — it is reachable from the tray, so
  it works even when the card itself cannot be clicked or found.
- **Scale** — 70% to 120% of the card, fonts, meters and padding together,
  for a widget that has to sit in a corner without taking the corner over.
  A Tk font is measured when the widget is built, so the card restarts
  itself to redraw at the new size — the same way the language switch does.
- **Tray icon** — draws one meter's percentage in that meter's own colour,
  so the number is readable with the card hidden. Which meter is a menu
  choice (**Tray shows**): session, week, Fable or Codex. Its tooltip carries
  the version.
- **Auto-away** — after N minutes of system-wide idle the bridge flips to
  away; only a **key press** flips it back (a nudged mouse does not).
  Manual switches are never undone automatically.

## Security notes

- `config.json` holds the bot token and is `.gitignore`d — a leaked token
  means someone else reads your notifications and can write into your group.
- The bridge sends work content (assistant text, errors, todo lists) to
  Telegram — only for projects you checked. `auto_discover: true` lifts
  that boundary for every session on the machine; enable it consciously.
- `spawn.enabled` (default `false`) starts a headless agent from a chat
  message — that is remote code execution on your machine by design, and a
  leaked token becomes the right to run an agent in your projects. Leave it off
  unless you fully understand the consequences.

## Documentation

- [INSTALL.md](INSTALL.md) — step by step, and what to check when it is quiet.
- [FEATURES.md](FEATURES.md) — every switch, what it sends and when.

## Tests

```bash
python tests/test_flow.py
```

Runs the whole flow against a fake Telegram API — no network, no token.

## Authors

- [Bonux](https://github.com/bonuxq)
- [treadstoneview](https://github.com/treadstoneview)

## License

[MIT](LICENSE)
