"""Self-update from GitHub Releases.

The installer already knows how to upgrade in place — same AppId, closes the
running copies, keeps config.json — so updating is: notice a newer tag, fetch
the setup binary, verify it, run it silently.

Two rules keep it from being a liability:

* it only ever runs from a frozen build (from source, `git pull` is the update);
* it refuses to install anything whose SHA-256 it could not confirm against
  the checksum published in the release notes. An unverified download is
  reported as an available update and left alone.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request

from .version import REPO, __version__, is_newer

API = "https://api.github.com/repos/{repo}/releases/latest"
ALLOWED_HOSTS = ("github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com")
TIMEOUT = 20


class UpdateError(Exception):
    """Anything that stops an update. Never fatal: the bridge keeps running."""


def _get(url, timeout=TIMEOUT):
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "claudetg-bridge",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError(str(e)[:200]) from None


def latest(repo=REPO):
    """The newest published release, or None when there are none yet."""
    try:
        data = json.loads(_get(API.format(repo=repo)).decode("utf-8", "replace"))
    except ValueError:
        raise UpdateError("release feed is not JSON") from None
    if not isinstance(data, dict) or not data.get("tag_name"):
        return None
    asset = next((a for a in data.get("assets") or []
                  if (a.get("name") or "").lower().endswith(".exe")), None)
    if not asset:
        return None
    return {
        "version": data["tag_name"],
        "url": asset.get("browser_download_url") or "",
        "name": asset.get("name") or "",
        "size": asset.get("size") or 0,
        "sha256": checksum_for(asset.get("name") or "", data.get("body") or ""),
        "notes": (data.get("body") or "").strip(),
    }


def checksum_for(name, notes):
    """Pull this asset's SHA-256 out of the release notes.

    Accepts the two shapes people actually write: `<hash>  <file>` as produced
    by sha256sum, and `SHA256: <hash>` on its own line.
    """
    if name:
        pair = re.search(r"\b([0-9a-fA-F]{64})\b[^\S\n]*[* ]?" + re.escape(name),
                         notes)
        if pair:
            return pair.group(1).lower()
    labelled = re.search(r"sha-?256\s*[:=]\s*([0-9a-fA-F]{64})\b", notes, re.I)
    return labelled.group(1).lower() if labelled else ""


def available(repo=REPO, current=__version__):
    """A release worth installing, or None."""
    release = latest(repo)
    if not release or not is_newer(release["version"], current):
        return None
    return release


def _safe_host(url):
    host = urllib.request.urlparse(url).hostname or ""
    return host in ALLOWED_HOSTS or host.endswith(".githubusercontent.com")


def download(release, into=None):
    """Fetch the installer and verify it. Returns the path on success."""
    url = release.get("url") or ""
    if not url.startswith("https://") or not _safe_host(url):
        raise UpdateError("refusing a download from %r" % url[:80])
    if not release.get("sha256"):
        raise UpdateError("the release notes carry no SHA-256 for this file")

    folder = into or tempfile.mkdtemp(prefix="claudetg-update-")
    path = os.path.join(folder, os.path.basename(release["name"] or "setup.exe"))
    payload = _get(url, timeout=300)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != release["sha256"]:
        raise UpdateError("checksum mismatch: got %s, expected %s"
                          % (digest[:12], release["sha256"][:12]))
    with open(path, "wb") as f:
        f.write(payload)
    return path


def launch(path):
    """Hand over to the installer and let go.

    It closes the widget and the daemon itself — including the process making
    this call — so nothing here waits for it to finish.
    """
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                     close_fds=True, creationflags=creation,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
