"""Reading the session transcript to recover what Claude actually said.

Hook payloads carry a `transcript_path` but not the assistant's text, so the
Stop handler pulls the last assistant turn out of the JSONL itself.
"""

import datetime as dt
import json
import os

TAIL_BYTES = 1_000_000


def _tail_lines(path, limit=TAIL_BYTES):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > limit:
            f.seek(size - limit)
            f.readline()  # discard the partial line at the seek point
        data = f.read()
    return data.decode("utf-8", errors="replace").splitlines()


def _entries(path):
    for line in _tail_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def text_of(entry):
    message = entry.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n\n".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ).strip()


def _parse_ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def summarize(path):
    """Return (assistant_text, turn_seconds). Both may be None/0 if unknown."""
    if not path or not os.path.exists(path):
        return "", None
    try:
        entries = list(_entries(path))
    except OSError:
        return "", None

    chunks, last_assistant_ts, turn_start = [], None, None
    for entry in entries:
        # Subagent traffic lives in the same file; it is not the main turn.
        if entry.get("isSidechain") or entry.get("isApiErrorMessage"):
            continue
        if entry.get("type") == "user" and not entry.get("isMeta"):
            # A tool result is also typed "user"; only real prompts start turns.
            content = (entry.get("message") or {}).get("content")
            is_tool_result = isinstance(content, list) and any(
                isinstance(p, dict) and p.get("type") == "tool_result" for p in content
            )
            if not is_tool_result:
                # A new prompt starts a new turn: drop what belonged to the
                # previous one so only the current turn is reported.
                turn_start = _parse_ts(entry.get("timestamp")) or turn_start
                chunks = []
        elif entry.get("type") == "assistant":
            candidate = text_of(entry)
            if candidate:
                # Keep every text block of the turn, not just the closing one:
                # the explanations between tool calls are the interesting part.
                chunks.append(candidate)
                last_assistant_ts = _parse_ts(entry.get("timestamp")) or last_assistant_ts

    seconds = None
    if turn_start and last_assistant_ts and last_assistant_ts >= turn_start:
        seconds = (last_assistant_ts - turn_start).total_seconds()
    return "\n\n".join(chunks), seconds
