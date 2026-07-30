# Claude Code ↔ Telegram bridge

**English** · [Українська](README.uk.md) · [Русский](README.ru.md)

Remote control for live Claude Code sessions from Telegram: questions arrive
as buttons, the assistant's text streams in as it is written, and your reply
in the chat resumes the work. Zero dependencies — Python 3.9+ standard
library only.

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
- **Rate-limit meters** (5-hour and weekly windows) after each turn and in
  the desktop widget, sourced from the Claude Code status line — no
  credentials, no scraping.
- **A desktop widget**: borderless always-on-top card with a presence toggle,
  usage meters (Standard ↔ Fable pools), a tray icon, transparency control
  and auto-away on keyboard idle.
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

- Python 3.9+ (no packages to install)
- Claude Code (the VSCode extension)
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- The daemon and hooks are plain Python; the widget, tray icon and autostart
  are **Windows-only** (ctypes / Win32)

## Install

The lazy path: open this folder in Claude Code and paste:

> Install the Claude Code ↔ Telegram bridge from this folder. Step by step:
> check `python --version` (3.9+), copy `config.example.json` to
> `config.json` and put in the bot token I am about to send, run
> `python install.py`, start `python -m claudetg.daemon` in the background,
> tell me to send `/register` in my Telegram group, wait for `chat_id` to
> appear in `config.json`, start `python widget.pyw`, run
> `python tests/test_flow.py` and show me the result. Do not print the token
> and never commit `config.json`.

Manual path:

1. **Bot**: message @BotFather, `/newbot`, keep the token.
2. **Group**: create a **supergroup**, enable **Topics** in its settings,
   add the bot, make it an **admin** (it needs *Manage topics*; admin status
   also lets it read plain messages, so BotFather's privacy mode is moot).
3. **Config**: `cp config.example.json config.json`, paste the token into
   `bot_token`. Leave `chat_id` alone — `/register` fills it in.
4. **Hooks**: `python install.py` — backs up `~/.claude/settings.json`,
   wires the hooks and the status line (the only source of rate-limit data),
   never duplicates itself, never touches foreign hooks.
   Undo: `python install.py --uninstall`.
5. **Run**: `python -m claudetg.daemon` and `python widget.pyw`.
   Autostart on Windows login: `python install.py --autostart`.
6. **Bind**: send `/register` in the group. Topics for projects are created
   on their first event.
7. **Pick projects** in the widget (right-click → Projects) or with
   `/projects` in the chat — no paths to type, the bridge lists every
   project Claude Code already knows. Unchecked projects send nothing.
8. **Verify**: `python tests/test_flow.py` — all green.

Restart any VSCode windows that were open during the install so they pick
up the hooks and the status line.

## Bot commands

| Command | Effect |
|---|---|
| `/register` | bind this group (once) |
| `/away`, `/here` | presence mode |
| `/status` | live sessions and what waits for an answer |
| `/projects` | toggle bridged projects |
| `/queue` | pending queued tasks |
| `/claude_status` | Anthropic service status |
| `/release` | release all pending waits back to VSCode |

## The widget

A frameless dark card, always on top. Drag anywhere to move, right-click
(or the tray icon, or the gear) for the menu.

- **Presence capsule** — the big button; green at the PC, amber away, red
  when the daemon is down (a click brings it up).
- **Usage meters** — the 5-hour and weekly windows with reset times, colored
  by severity; the Standard ↔ Fable switch picks which limit pool is shown.
- **Menu** — Projects, Features (every daemon toggle, applied instantly),
  transparency presets, auto-away timing, hide-to-tray.
- **Auto-away** — after N minutes of system-wide idle the bridge flips to
  away; only a **key press** flips it back (a nudged mouse does not).
  Manual switches are never undone automatically.

## Security notes

- `config.json` holds the bot token and is `.gitignore`d — a leaked token
  means someone else reads your notifications and can write into your group.
- The bridge sends work content (assistant text, errors, todo lists) to
  Telegram — only for projects you checked. `auto_discover: true` lifts
  that boundary for every session on the machine; enable it consciously.
- `spawn.enabled` (default `false`) would start a headless agent from a chat
  message — that is remote code execution on your machine by design; leave
  it off unless you fully understand the consequences.

## Tests

```bash
python tests/test_flow.py
```

Runs the whole flow against a fake Telegram API — no network, no token.

## License

[MIT](LICENSE)
