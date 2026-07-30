# Install

**English** · [Українська](INSTALL.uk.md) · [Русский](INSTALL.ru.md)

## The short version

1. Download **`ClaudeTelegram-Setup.exe`** from the
   [releases page](https://github.com/bonuxq/claude-telegram-bridge/releases)
   and run it.
2. The widget opens a setup screen. Follow the five steps in it.

That is the whole installation. No Python, no terminal, no editing files —
the installer carries its own runtime and wires the Claude Code hooks itself.

Windows only. The daemon and the hooks are plain Python and would run
anywhere, but the widget is built on Win32 APIs and there is no port.

---

## What the installer does

- Installs into `%LOCALAPPDATA%\ClaudeTelegram` — **no administrator rights**.
  It writes nothing outside your own profile.
- Wires the Claude Code hooks and the status line into
  `~/.claude/settings.json`, keeping a timestamped backup and leaving any
  hooks of your own alone.
- Optionally starts the bridge at login (a checkbox during setup).
- Starts the daemon and the widget when it finishes.

Uninstalling removes the hooks again and deletes the program, but keeps
`config.json`: a reinstall should not cost you the token and the project list.

---

## The setup screen

It opens by itself on a fresh install, and lives in the widget's menu under
**Telegram…** afterwards.

### 1. Create the bot

Press **Open @BotFather**, send it `/newbot`, pick a display name and a
username. It answers with a token that looks like `1234567890:AAF...`.

### 2. Paste the token

Into the field, then **Save and check**. The bridge asks Telegram whether the
token works before storing it, so you find out immediately:

- `Bot @yourbot is connected` — done.
- `Telegram did not accept this token` — it is wrong or expired; the bridge
  keeps the old one rather than saving a broken one.

### 3. Create the group

A **supergroup**, not a plain group or a channel. In its settings turn on
**Topics** — the bridge gives every project a topic of its own, and without
them there is nothing to separate.

### 4. Add the bot as an admin

Without admin rights it cannot create topics. Admin status also lets it read
plain messages, which is why BotFather's privacy setting does not matter.

### 5. Bind the group

Send `/register` in the group. The bot confirms, and the **Claude status** and
**Claude limits** topics appear by themselves.

Binding is one-way on purpose: once bound, `/register` from any other chat is
ignored, so finding your bot is not enough to redirect its reports. To move
the bridge to another group, send `/unregister` in the current one first.

### 6. Hooks

The setup screen shows whether they are in place and installs them with one
button. The installer already did this, so normally it just reads
`Claude Code hooks are installed`.

**Restart any VSCode windows that were open during the install** — they pick
up the hooks on start.

---

## Updates

The installed build keeps itself current: it checks GitHub for releases and
installs them when the bridge is idle, never mid-session. Turn it off in
Features → **Update automatically** if you would rather do it by hand.

Publishing a release yourself: run `python build.py`, upload the setup file,
and paste the checksum line it prints into the release notes — the updater
refuses anything it cannot verify.

## Choosing projects

No paths to type. Widget menu → **Projects…**, or `/projects` in the chat:
you get the list of projects Claude Code already knows, newest first. The
checked ones get their own Telegram topic.

That list is the privacy boundary — **an unchecked project sends nothing at
all**.

---

## Running from source instead

If you would rather not run a binary:

```bash
git clone https://github.com/bonuxq/claude-telegram-bridge
cd claude-telegram-bridge
cp config.example.json config.json
python install.py                 # hooks + status line
python -m claudetg.daemon         # the daemon
python widget.pyw                 # the widget
```

Python 3.9+ and nothing else — the whole bridge is standard library. The
setup screen works exactly the same way from source.

Building the installer yourself:

```bash
pip install pyinstaller
python build.py
```

---

## When something does not work

**Nothing arrives in Telegram.** Check the setup screen first: it says
whether the token is accepted, whether a group is bound, and whether the
hooks are installed. Those three cover almost everything.

**Hooks do not fire.** A VSCode window opened before the install usually
picks them up on the fly, but not always. Restart the window.

**The project topic never appeared.** A topic is created on the first event
from a session. Inside a long turn there are no events — the bridge speaks at
turn boundaries.

**The limits stay empty.** Claude Code only renders the status line in its
terminal UI, so a VSCode-only setup never feeds it. Turn on **Poll the limits
myself** in Features — the daemon then asks for them directly every 30
seconds. See [FEATURES.md](FEATURES.md#when-the-status-line-never-runs--usage_poll).

**The daemon does not answer.** Look in `daemon.log` next to the executable.
If port 8787 is busy, the daemon is already running and a second one is not
needed. The widget logs its own crashes to `widget.log` in the same folder.

**Windows says "unknown publisher".** The build is not code-signed yet. You
can check the sources and build it yourself with the commands above.

---

## Security

- `config.json` holds the bot token. A leaked token lets someone else read
  your notifications and write into your group.
- The bridge sends work content to Telegram: answer text, errors, todo lists.
  Only **checked** projects reach the chat.
- `auto_discover` bridges every project automatically — convenient, but then
  the contents of every session on the machine go out.
- `spawn` (off by default) starts an agent on a message from Telegram. That
  is code execution on your machine driven by a chat message, so a leaked
  token becomes the right to run an agent in your projects.
- `usage_poll` (off by default) is the only part that touches a credential:
  it reads the Claude Code OAuth token to ask Anthropic for your limits.
