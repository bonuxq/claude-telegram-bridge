"""Watch status.claude.com so an outage arrives as a message, not a surprise.

It is a Statuspage instance: `summary.json` carries the overall indicator, the
per-component states and the open incidents in one request.
"""

import json
import time
import urllib.error
import urllib.request

from .i18n import t

SUMMARY_URL = "https://status.claude.com/api/v2/summary.json"

INDICATOR_ICON = {"none": "🟢", "minor": "🟡", "major": "🟠", "critical": "🔴"}
COMPONENT_ICON = {
    "operational": "🟢",
    "degraded_performance": "🟡",
    "partial_outage": "🟠",
    "major_outage": "🔴",
    "under_maintenance": "🔧",
}
IMPACT_ICON = {"none": "⚪", "minor": "🟡", "major": "🟠", "critical": "🔴"}


def fetch(timeout=20):
    req = urllib.request.Request(SUMMARY_URL, headers={"User-Agent": "claudetg"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def snapshot(summary, watched=None):
    """Reduce the payload to the few facts worth comparing between polls."""
    components = {}
    for c in summary.get("components") or []:
        name = c.get("name")
        if c.get("group") or not name:
            continue  # group headers duplicate their children
        if watched and name not in watched:
            continue
        components[name] = c.get("status")
    incidents = {
        i.get("id"): {
            "name": i.get("name"),
            "status": i.get("status"),
            "impact": i.get("impact"),
            "url": i.get("shortlink"),
            "update": _latest_update(i),
        }
        for i in (summary.get("incidents") or [])
        if i.get("id")
    }
    return {
        "indicator": (summary.get("status") or {}).get("indicator"),
        "description": (summary.get("status") or {}).get("description"),
        "components": components,
        "incidents": incidents,
    }


def _latest_update(incident):
    updates = incident.get("incident_updates") or []
    if not updates:
        return ""
    return (updates[0].get("body") or "").strip()


def diff(old, new):
    """Return a list of markdown messages describing what changed."""
    if not old:
        return []  # first poll only establishes the baseline
    messages = []

    if old.get("indicator") != new.get("indicator"):
        icon = INDICATOR_ICON.get(new.get("indicator"), "⚪")
        messages.append(f"{icon} **Claude: {new.get('description')}**")

    for name, status in new.get("components", {}).items():
        before = old.get("components", {}).get(name)
        if before is not None and before != status:
            icon = COMPONENT_ICON.get(status, "⚪")
            messages.append(t("component.change", icon=icon, name=name,
                              state=state_name(status),
                              before=state_name(before)))

    old_incidents = old.get("incidents", {})
    for iid, data in new.get("incidents", {}).items():
        previous = old_incidents.get(iid)
        if previous and previous.get("status") == data.get("status"):
            continue
        icon = IMPACT_ICON.get(data.get("impact"), "⚪")
        head = (t("incident.new") if not previous
                else t("incident.update", status=data.get("status")))
        lines = [f"{icon} **{data.get('name')}** — {head}"]
        if data.get("update"):
            lines.append(data["update"][:1200])
        if data.get("url"):
            lines.append(data["url"])
        messages.append("\n\n".join(lines))

    for iid, data in old_incidents.items():
        if iid not in new.get("incidents", {}):
            messages.append(t("incident.closed", name=data.get("name")))

    return messages


_STATES = ("operational", "degraded_performance", "partial_outage",
           "major_outage", "under_maintenance")


def state_name(status):
    if status in _STATES:
        return t("state." + status)
    return status or "?"


class Monitor:
    def __init__(self, daemon, interval=300, watched=None):
        self.daemon = daemon
        self.interval = max(60, int(interval))
        self.watched = set(watched) if watched else None

    def run_forever(self):
        while True:
            # Checked per cycle so the widget toggle applies without a restart.
            if not (self.daemon.cfg.get("status_monitor") or {}).get("enabled", True):
                time.sleep(self.interval)
                continue
            try:
                self.poll_once()
            except (urllib.error.URLError, OSError, ValueError) as e:
                self.daemon.log_status(f"status fetch failed: {type(e).__name__} {e}")
            except Exception as e:
                self.daemon.log_status(f"status monitor error: {type(e).__name__} {e}")
            time.sleep(self.interval)

    def poll_once(self):
        current = snapshot(fetch(), self.watched)
        previous = self.daemon.status_snapshot()
        for message in diff(previous, current):
            self.daemon.announce_status(message)
        self.daemon.store_status_snapshot(current)
