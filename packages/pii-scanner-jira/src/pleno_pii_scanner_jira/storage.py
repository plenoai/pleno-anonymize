"""Storage XHTML -> plain-text converter for Data Center Jira.

Data Center Jira (`/rest/api/2`) returns the issue `description` and
comment bodies as a fragment of XHTML — wiki-style markup serialised
into HTML for transport. We strip every tag and keep the text content
so detectors see the same surface text a Jira reader would.

Why a hand-rolled parser instead of `BeautifulSoup` / `lxml`:

* The package goal is zero non-essential transitive dependencies; one
  more wheel multiplies the supply-chain audit surface for enterprise
  customers (ADR-0007 §13).
* The input fragment is not a full HTML document — feeding it to a
  full HTML5 parser invokes implicit `<html>`/`<body>` insertion that
  changes the offsets we'd want for future highlight rendering.
* The conversion is one-way and lossy on purpose; we only need the
  textual leaves.

The implementation uses Python's stdlib `html.parser.HTMLParser`,
which is forgiving (treats unclosed tags as text), handles entities,
and is part of the same security-update cycle as Python itself.
"""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser


# Block-level tags that should produce a newline boundary so detectors
# do not see neighbouring blocks as one run-on sentence. The list
# matches the storage-XHTML elements Jira DC actually emits — extra
# tags here cost nothing, missing ones merge sibling block text.
_BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ul",
        "ol",
        "table",
        "tr",
        "blockquote",
        "pre",
        "hr",
    }
)


# Tags whose text content we drop entirely. `<script>` / `<style>` are
# defence in depth — Jira DC strips them server-side but a
# misconfigured plugin or a custom field could let one through.
_SKIP_TAGS: frozenset[str] = frozenset({"script", "style"})


class _StorageStripper(HTMLParser):
    """HTMLParser subclass that accumulates text content + block breaks.

    `convert_charrefs=True` (the default in Python 3.5+) turns
    `&amp;` into `&` automatically; we additionally call `html.unescape`
    on the assembled output as a belt-and-suspenders measure for
    references the parser might miss in malformed input.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # Counter of currently-open `<script>` / `<style>` tags. We use
        # a counter rather than a flag because nested skip tags (rare
        # but legal) would otherwise unset on the inner `</script>` and
        # re-emit the surrounding text.
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in _BLOCK_TAGS:
            # Block boundary: ensure the previous run is followed by a
            # newline so detector input has paragraph breaks.
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing tags (`<br/>`, `<hr/>`) — treat as a block break.
        del attrs
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        self._parts.append(data)

    def render(self) -> str:
        # Collapse consecutive blank lines so a long stream of <br/>
        # tags does not balloon the detector input. We keep one blank
        # line as a paragraph separator; downstream NER batches on
        # paragraph boundaries.
        joined = unescape("".join(self._parts))
        out_lines: list[str] = []
        prev_blank = False
        for line in joined.splitlines():
            stripped = line.rstrip()
            if not stripped:
                if prev_blank:
                    continue
                prev_blank = True
                out_lines.append("")
            else:
                prev_blank = False
                out_lines.append(stripped)
        return "\n".join(out_lines).strip()


def storage_to_text(html: object) -> str:
    """Render a Jira DC storage-XHTML fragment as plain text.

    `html` is typed `object` because Jira's response shape varies:
    some custom fields return `None`, some return raw strings, others
    return a `{ "value": "...", "representation": "html" }` wrapper.
    Callers can pass any of these and we fish out the string content.
    """
    if isinstance(html, str):
        text = html
    elif isinstance(html, dict):
        # `body.storage.value` — both the comment storage shape and the
        # custom-field storage shape land here.
        v = html.get("value")
        if isinstance(v, str):
            text = v
        else:
            return ""
    else:
        return ""
    if not text:
        return ""
    parser = _StorageStripper()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # HTMLParser raises on truly broken input (rare). Falling back
        # to the unescape() of the raw string is better than dropping
        # the entire body — detectors can still match on the raw text.
        return unescape(text)
    return parser.render()


__all__ = ["storage_to_text"]
