# Code signing on Windows

The Windows build is Authenticode-signed as **Bonux Apps** — certificate
subject `CN=Oleksii Holiuk, O=Oleksii Holiuk, L=Kyiv, S=Kyiv, C=UA`, issued by
`Certum Code Signing 2021 CA`.

| | |
|---|---|
| Thumbprint | `D875328A72DF60A5BD74CB38E418C54B16A60BE9` |
| Key | RSA 4096, `sha256RSA` |
| EKU | `1.3.6.1.5.5.7.3.3` (Code Signing) |
| Valid | 2026-10-26 … 2027-10-26 |
| Timestamps | RFC3161, `http://time.certum.pl` |

**There is no private key on this machine, and there is not supposed to be
one.** It stays in Certum's cloud; SimplySign Desktop opens a session against
it and publishes the certificate into `CurrentUser\My` for as long as that
session lasts. So there is no PFX to guard, nothing to export, and a stolen
checkout buys nobody a signature. The only thing a new machine needs is the
SimplySign client and the account the key belongs to.

## Building a signed release

```
python build.py
```

Signing is the default, and a missing certificate stops the build. That is
deliberate: a signed build and an unsigned one are indistinguishable from the
command line, so the one that ships unnoticed is always the unsigned one.
`python build.py --no-sign` is there for working on the build itself and says
plainly what it produced.

The order matters and is the reason signing is not simply bolted onto the end:

1. PyInstaller writes the one-folder tree into `dist\ClaudeTelegram`.
2. The four executables are signed **before** Inno Setup packs them, or the
   installer would carry unsigned copies of binaries that look signed on disk.
3. Inno Setup signs two things only it can produce — the setup binary and the
   uninstaller stub it writes into the install directory. It gets the command
   from `scripts\Sign-Windows.ps1 -InnoCommand`, so the tool path, the
   thumbprint and the timestamp URL are written down in exactly one place.
4. Everything is verified again, and the build fails if anything is not
   validly signed by us and timestamped.

## What is signed, and what is deliberately not

Signed — ours:

```
ClaudeTelegram.exe      claudetg-daemon.exe
hookc.exe               statusline.exe
ClaudeTelegram-Setup-<version>.exe      unins000.exe (written at install time)
```

Not signed, on purpose: everything under `_internal`. Those 62 files are the
CPython runtime, Tcl/Tk and the C runtime, already carrying valid signatures
from the Python Software Foundation and from Microsoft. Re-signing them would
replace a publisher's attestation with ours on code we did not write, and
would gain nothing — Windows checks the executable that runs, not the
libraries it loads.

## Verifying

```
powershell -ExecutionPolicy Bypass -File scripts\Sign-Windows.ps1 -VerifyOnly
```

which runs `signtool verify /pa /all /tw /v` and then checks, per file, that
Authenticode reports `Valid`, that the signer is our thumbprint and not merely
*some* trusted publisher, and that a timestamp is present. By hand:

```
signtool verify /pa /all /tw /v ClaudeTelegram-Setup-1.0.16.exe
certutil -hashfile ClaudeTelegram-Setup-1.0.16.exe SHA256
```

A timestamp is what keeps a release verifiable after the certificate expires:
without one, every binary ever shipped stops validating on the expiry date.

## On a machine that has never signed anything

1. Install **proCertum SmartSign SimplySign Desktop** (9.4.5 or newer).
2. Open it and use **Connect to SimplySign** with the account the certificate
   belongs to. The one-time token comes from the SimplySign mobile app and is
   typed into Certum's own window — never into a terminal, a script, or a
   chat.
3. Install the Windows SDK's *Signing Tools for Desktop Apps*; the script
   finds the newest `x64\signtool.exe` under `Windows Kits\10\bin` by itself.
4. `python build.py`. No CSR, no new certificate, no PFX.

If the session has lapsed the certificate is simply absent from
`CurrentUser\My`, and the build says so and stops rather than quietly
producing something unsigned.

## When it expires

The certificate runs to **2027-10-26**. Renewal is done in Certum's panel and
the thumbprint changes — update it in `scripts\Sign-Windows.ps1` (the default
for `-Thumbprint`) and in the table at the top of this file. Already-published
releases keep validating, because they are timestamped.

## Other platforms

This project ships a Windows build and nothing else: the installer is Inno
Setup and the code is `ctypes.windll` throughout. The Bonux Apps GPG key and
an Apple Developer ID do not apply to anything here until there is a Linux or
macOS artifact to sign.
