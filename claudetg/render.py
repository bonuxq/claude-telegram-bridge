"""Markdown -> Telegram HTML, plus splitting that never cuts a code block open.

Telegram's MarkdownV2 requires escaping a dozen characters and fails the whole
message on a single miss; HTML only needs &, < and > escaped, so everything is
converted to HTML instead.
"""

import html
import re

from .i18n import t

LIMIT = 3900  # Telegram hard limit is 4096; leave room for headers/footers.

_FENCE = re.compile(r"^[ \t]*```([\w+-]*)[ \t]*$", re.M)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_HEADER = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*$", re.M)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_PLACEHOLDER = "\x00{}\x00"


_TABLE_ROW = re.compile(r"^\s*\|.*\|?\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _format_table(rows):
    """Telegram has no tables; monospace with padded columns is the honest
    substitute, and it survives narrow phone screens better than raw pipes."""
    grid = [_cells(r) for r in rows]
    grid = [r for i, r in enumerate(grid) if not (i == 1 and _TABLE_SEP.match(rows[i]))]
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    sizes = [max(len(r[c]) for r in grid) for c in range(width)]
    out = []
    for i, row in enumerate(grid):
        out.append("  ".join(cell.ljust(sizes[c]) for c, cell in enumerate(row)).rstrip())
        if i == 0:
            out.append("  ".join("-" * sizes[c] for c in range(width)))
    return "\n".join(out)


def extract_tables(text):
    """Yield ('text', body) / ('table', body) parts of a prose block."""
    parts, buf, table = [], [], []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        looks_like_row = _TABLE_ROW.match(line) and line.count("|") >= 2
        if looks_like_row:
            table.append(line)
            continue
        if len(table) >= 2:
            if buf:
                parts.append(("text", "\n".join(buf)))
                buf = []
            parts.append(("table", _format_table(table)))
        else:
            buf.extend(table)
        table = []
        buf.append(line)
    if len(table) >= 2:
        if buf:
            parts.append(("text", "\n".join(buf)))
            buf = []
        parts.append(("table", _format_table(table)))
    else:
        buf.extend(table)
    if buf:
        parts.append(("text", "\n".join(buf)))
    return parts


def split_blocks(text):
    """Split markdown into ('text', body) and ('code', lang, body) blocks."""
    blocks = []
    pos = 0
    while True:
        open_m = _FENCE.search(text, pos)
        if not open_m:
            break
        before = text[pos:open_m.start()]
        close_m = _FENCE.search(text, open_m.end())
        if not close_m:
            # Unterminated fence (truncated output): treat the rest as code
            # rather than leaking raw backticks into the message.
            if before.strip():
                blocks.append(("text", before))
            blocks.append(("code", open_m.group(1) or "", text[open_m.end():].strip("\n")))
            return blocks
        if before.strip():
            blocks.append(("text", before))
        blocks.append(
            ("code", open_m.group(1) or "", text[open_m.end():close_m.start()].strip("\n"))
        )
        pos = close_m.end()
    tail = text[pos:]
    if tail.strip():
        blocks.append(("text", tail))
    return blocks


def inline_html(text):
    """Escape and apply inline markdown. Code spans are shielded first so that
    ** or * inside them is not mistaken for emphasis."""
    spans = []

    def stash(m):
        spans.append(html.escape(m.group(1)))
        return _PLACEHOLDER.format(len(spans) - 1)

    text = _INLINE_CODE.sub(stash, text)
    text = html.escape(text)
    text = _HEADER.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    for i, code in enumerate(spans):
        text = text.replace(_PLACEHOLDER.format(i), f"<code>{code}</code>")
    return text.strip()


def code_html(lang, body):
    body = html.escape(body)
    if lang:
        return f'<pre><code class="language-{html.escape(lang)}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def _split_long_code(lang, body, limit):
    """Break an oversized code block into several complete <pre> blocks."""
    # Budget for the wrapper tags, worst case with a language class.
    room = limit - len(code_html(lang, "")) - 16
    lines, chunk, out = body.split("\n"), [], []
    size = 0
    for line in lines:
        # A single monstrous line still has to be cut somewhere.
        while len(line) > room:
            if chunk:
                out.append(code_html(lang, "\n".join(chunk)))
                chunk, size = [], 0
            out.append(code_html(lang, line[:room]))
            line = line[room:]
        if size + len(line) + 1 > room and chunk:
            out.append(code_html(lang, "\n".join(chunk)))
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        out.append(code_html(lang, "\n".join(chunk)))
    return out


def _split_long_text(body, limit):
    """Break oversized prose on paragraph, then line, then hard boundaries."""
    out, buf = [], ""
    for para in body.split("\n\n"):
        piece = inline_html(para)
        if not piece:
            continue
        if len(piece) > limit:
            if buf:
                out.append(buf)
                buf = ""
            for line in piece.split("\n"):
                while len(line) > limit:
                    out.append(line[:limit])
                    line = line[limit:]
                if len(buf) + len(line) + 1 > limit:
                    out.append(buf)
                    buf = line
                else:
                    buf = f"{buf}\n{line}" if buf else line
            continue
        if len(buf) + len(piece) + 2 > limit:
            out.append(buf)
            buf = piece
        else:
            buf = f"{buf}\n\n{piece}" if buf else piece
    if buf:
        out.append(buf)
    return out


def render(text, header=None, limit=LIMIT):
    """Return a list of HTML message bodies, each within Telegram's limit."""
    if header:
        # Reserve room up front: the header is prepended after splitting, and
        # the " · nn/nn" counter is only known once the split is done.
        limit -= len(header) + len(" · 99/99") + 2
    pieces = []
    for block in split_blocks(text or ""):
        if block[0] == "code":
            _, lang, body = block
            rendered = code_html(lang, body)
            pieces.extend(
                [rendered] if len(rendered) <= limit else _split_long_code(lang, body, limit)
            )
        else:
            for kind, body in extract_tables(block[1]):
                if kind == "table":
                    rendered = code_html("", body)
                    pieces.extend([rendered] if len(rendered) <= limit
                                  else _split_long_code("", body, limit))
                else:
                    pieces.extend(_split_long_text(body, limit))

    messages, buf = [], ""
    for piece in pieces:
        if len(buf) + len(piece) + 2 > limit:
            if buf:
                messages.append(buf)
            buf = piece
        else:
            buf = f"{buf}\n\n{piece}" if buf else piece
    if buf:
        messages.append(buf)
    if not messages:
        messages = [f"<i>{t('render.empty')}</i>"]

    if header:
        head = header if len(messages) == 1 else f"{header} · 1/{len(messages)}"
        messages[0] = f"{head}\n\n{messages[0]}"
        for i in range(1, len(messages)):
            messages[i] = f"{header} · {i + 1}/{len(messages)}\n\n{messages[i]}"
    return messages


def duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return t("dur.s", n=seconds)
    if seconds < 3600:
        return t("dur.ms", m=seconds // 60, s=f"{seconds % 60:02d}")
    return t("dur.hm", h=seconds // 3600, m=f"{(seconds % 3600) // 60:02d}")


def header(status, project, seconds=None):
    parts = [status, f"<b>{html.escape(project)}</b>"]
    if seconds is not None:
        parts.append(f"· {duration(seconds)}")
    return " ".join(parts)
