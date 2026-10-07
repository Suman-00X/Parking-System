"""Renders the project's own Markdown documents (FEATURES.md, API.md) to HTML for the public pages.

Order: split the text into blocks (fenced code, headings, tables, lists, block quotes, rules,
paragraphs) -> render inline marks inside each (_inline: code, bold, italic, links) -> collect the
headings with stable ids for a table of contents (render returns both). Covers the subset those
documents use, not all of CommonMark. All text is escaped before any markup is added, and only
http(s), relative and in-page links are kept.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^```\s*([\w+-]*)\s*$")
_LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_CODE_SPAN = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_COMMENT = re.compile(r"<!--.*?-->", re.S)


@dataclass(slots=True)
class Rendered:
    html: str
    toc: list[tuple[int, str, str]] = field(default_factory=list)
    """(level, id, text) for every heading, in order."""


def slug(text: str) -> str:
    """The anchor GitHub gives a heading, so in-document links keep working."""
    plain = re.sub(r"[`*_\[\]()]", "", text).strip().lower()
    plain = re.sub(r"[^\w\s-]", "", plain)
    return re.sub(r"\s", "-", plain)


#: Maps a relative link in a document to where it should point on the site, or None to drop the link and keep its text.
LinkMap = Callable[[str], str | None]


def _safe_href(href: str, link_base: str | LinkMap) -> str | None:
    if href.startswith(("http://", "https://", "#", "/")):
        return href
    if re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I):
        return None  # javascript:, data: and the like
    return link_base(href) if callable(link_base) else link_base + href


def _inline(text: str, link_base: str | LinkMap) -> str:
    """Escape, then add code spans, links, bold and italics; code spans are protected from the rest."""
    codes: list[str] = []

    def keep_code(match: re.Match[str]) -> str:
        codes.append(f"<code>{html.escape(match.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE_SPAN.sub(keep_code, text)
    text = html.escape(text, quote=False)

    def link(match: re.Match[str]) -> str:
        label, href = match.group(1), html.unescape(match.group(2))
        safe = _safe_href(href, link_base)
        if safe is None:
            return label
        external = safe.startswith("http")
        return f'<a href="{html.escape(safe)}"{' rel="noopener" target="_blank"' if external else ""}>{label}</a>'

    text = _LINK.sub(link, text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", row)]


def render(text: str, *, link_base: str | LinkMap = "") -> Rendered:
    """Markdown to HTML; `link_base` prefixes relative links, or maps each one, so repository paths can point somewhere real."""
    lines = _COMMENT.sub("", text).splitlines()
    out: list[str] = []
    toc: list[tuple[int, str, str]] = []
    used: dict[str, int] = {}
    i = 0
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(p.strip() for p in paragraph), link_base)}</p>")
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush()
            i += 1
            continue
        fence = _FENCE.match(line.strip())
        if fence:
            flush()
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            language = f' class="language-{html.escape(fence.group(1))}"' if fence.group(1) else ""
            out.append(f"<pre><code{language}>{html.escape(chr(10).join(body))}</code></pre>")
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            level, title = len(heading.group(1)), heading.group(2)
            anchor = slug(title)
            if anchor in used:
                used[anchor] += 1
                anchor = f"{anchor}-{used[anchor]}"
            else:
                used[anchor] = 0
            toc.append((level, anchor, re.sub(r"[`*]", "", title)))
            out.append(f'<h{level} id="{anchor}"><a class="anchor" href="#{anchor}" aria-hidden="true">#</a>{_inline(title, link_base)}</h{level}>')
            i += 1
            continue
        if _HR.match(line):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1]):
            flush()
            head = _cells(line)
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            thead = "".join(f"<th>{_inline(c, link_base)}</th>" for c in head)
            tbody = "".join("<tr>" + "".join(f"<td>{_inline(c, link_base)}</td>" for c in row) + "</tr>" for row in rows)
            out.append(f'<div class="table-wrap"><table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table></div>')
            continue
        if line.lstrip().startswith(">"):
            flush()
            quoted = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quoted.append(lines[i].lstrip()[1:].removeprefix(" "))
                i += 1
            out.append(f"<blockquote>{render(chr(10).join(quoted), link_base=link_base).html}</blockquote>")
            continue
        if _LIST_ITEM.match(line):
            flush()
            block = []
            while i < len(lines) and (_LIST_ITEM.match(lines[i]) or (lines[i].startswith("  ") and lines[i].strip())):
                block.append(lines[i])
                i += 1
            out.append(_list(block, link_base))
            continue
        paragraph.append(line)
        i += 1
    flush()
    return Rendered(html="\n".join(out), toc=toc)


def _list(block: list[str], link_base: str | LinkMap) -> str:
    """A list, nesting by indentation; continuation lines join the item above."""
    items: list[tuple[int, bool, str]] = []
    for raw in block:
        match = _LIST_ITEM.match(raw)
        if match:
            items.append((len(match.group(1).expandtabs(4)), match.group(2)[0].isdigit(), match.group(3)))
        elif items:
            depth, ordered, text = items[-1]
            items[-1] = (depth, ordered, f"{text} {raw.strip()}")
    html_out: list[str] = []
    stack: list[tuple[int, str]] = []
    for depth, ordered, text in items:
        tag = "ol" if ordered else "ul"
        while stack and depth < stack[-1][0]:
            html_out.append(f"</li></{stack.pop()[1]}>")
        if stack and depth == stack[-1][0] and tag != stack[-1][1]:
            # A numbered list right after a bulleted one at the same depth is a new list.
            html_out.append(f"</li></{stack.pop()[1]}>")
        if not stack or depth > stack[-1][0]:
            html_out.append(f"<{tag}>")
            stack.append((depth, tag))
        else:
            html_out.append("</li>")
        checkbox = re.match(r"^\[( |x)\]\s+(.*)$", text)
        if checkbox:
            mark = '<span class="check done" aria-label="done">✓</span>' if checkbox.group(1) == "x" else '<span class="check" aria-label="open">○</span>'
            text = checkbox.group(2)
            html_out.append(f'<li class="task">{mark}{_inline(text, link_base)}')
        else:
            html_out.append(f"<li>{_inline(text, link_base)}")
    while stack:
        html_out.append(f"</li></{stack.pop()[1]}>")
    return "".join(html_out)
