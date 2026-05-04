"""Storage-format XHTML → plain text conversion for Confluence pages.

Confluence stores page bodies in a custom XHTML dialect ("storage
format"). It mixes vanilla HTML (`<p>`, `<ul>`, `<a>`, `<table>`,
`<code>`) with namespaced macros under the `ac:` (Atlassian Confluence)
and `ri:` (Resource Identifier) namespaces. The macros wrap operator-
authored prose — `<ac:structured-macro ac:name="info">` for info
panels, `<ac:structured-macro ac:name="code">` for code blocks, etc.
The user-visible text inside a macro is conventionally under
`<ac:rich-text-body>` (free-form prose) or `<ac:plain-text-body>`
(verbatim, often wrapped in CDATA).

The detector pipeline only cares about the surface text the operator
sees — no formatting, no macro names. We therefore:

1. Wrap the body in a synthetic root with the required namespace
   declarations so `xml.etree.ElementTree` can parse it (otherwise
   prefixed elements raise `unbound prefix`).
2. Walk the tree, extracting text nodes, joining sibling blocks with
   newlines so paragraphs / list items stay separated.
3. On any parse failure (Confluence has historically emitted invalid
   XHTML — bare `&nbsp;` entities, unclosed `<br>`, mismatched macro
   end tags) fall back to a tag-stripping regex so we never lose a
   page's text just because the storage parser got upset.

This is intentionally not a full HTML→Markdown converter; the detector
runs on raw text and treats markup as noise.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import Element


# Namespace declarations Confluence's storage format relies on but does
# not always emit on the body root. We prepend a synthetic wrapper with
# all of them so `ElementTree` can resolve every prefix the parser will
# encounter; without these, `<ac:structured-macro>` raises
# `xml.etree.ElementTree.ParseError: unbound prefix`. The URIs match
# Atlassian's documented XHTML schema; if they ever change, only the
# wrapper needs updating.
_NAMESPACE_DECLS = (
    'xmlns:ac="http://atlassian.com/content"'
    ' xmlns:ri="http://atlassian.com/resource/identifier"'
    ' xmlns:atlassian-content="http://atlassian.com/content"'
)


# Block-level tags whose text we want to be newline-separated from
# siblings so that detectors (which often rely on line context) see
# paragraphs / list items as distinct lines. Keeping this as a frozen
# set lets the walker do an O(1) check per element without re-allocating
# a tuple per call.
_BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "td",
        "th",
        "pre",
        "code",
        "blockquote",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        # Macro structural elements: each rich-text body is its own
        # block, and each macro emits at least one logical paragraph.
        "structured-macro",
        "rich-text-body",
        "plain-text-body",
        "task",
        "task-body",
        "layout-section",
        "layout-cell",
    }
)


# Tags whose contents we drop entirely. `parameter` macro arguments are
# config (e.g. `language=java` for a code-block macro) and never user-
# visible text; they would otherwise leak macro internals into the
# detector pipeline as noise. `ri:user`, `ri:page`, `ri:attachment` etc.
# are all empty self-closing references — their attributes carry the
# pointer; we surface the human surface text via the surrounding
# rich-text-body, not the reference itself.
_DROP_TAGS: frozenset[str] = frozenset({"parameter"})


# Confluence emits a surprising amount of `&nbsp;` and other named
# entities that ElementTree's strict XML parser rejects. We replace the
# known-common ones up-front; uncommon ones fall through to the
# tag-strip fallback. `&nbsp;` is the only one Confluence emits in raw
# storage in practice — the rest are already numeric refs by the time
# they hit the wire.
_ENTITY_FIXUPS: tuple[tuple[str, str], ...] = (
    ("&nbsp;", " "),
)


def storage_to_text(body: str | None) -> str:
    """Convert a Confluence storage-format XHTML body to plain text.

    Returns the empty string for None/empty input. On any parser
    failure falls through to `_tag_strip` so a malformed body still
    produces a best-effort text payload — losing the body silently
    would mean a finding-rich page goes un-scanned, which is worse than
    a slightly noisier text extraction.
    """
    if not body:
        return ""
    fixed = body
    for needle, replacement in _ENTITY_FIXUPS:
        fixed = fixed.replace(needle, replacement)
    wrapped = f"<root {_NAMESPACE_DECLS}>{fixed}</root>"
    try:
        root = ET.fromstring(wrapped)
    except ET.ParseError:
        # ElementTree gives up on any non-strict-XML quirk (unclosed
        # `<br>`, mismatched macro tags, bare ampersands inside an
        # attribute). Drop to the regex fallback so the operator's
        # findings are not gated on the upstream's XHTML hygiene.
        return _tag_strip(body)
    parts: list[str] = []
    _walk(root, parts)
    return _normalise_whitespace("\n".join(parts))


def _walk(element: Element, out: list[str]) -> None:
    """Depth-first text walk that respects block boundaries.

    Each block-level element flushes its accumulated text into `out`
    as a single line (subject to `_normalise_whitespace`). Inline
    elements append text into the current line. Dropped tags
    contribute nothing.
    """
    local = _local_name(element.tag)
    if local in _DROP_TAGS:
        return
    is_block = local in _BLOCK_TAGS
    # Element.text is the text *before* the first child; tail is the
    # text *after* this element closes (sibling whitespace). We collect
    # both at the appropriate boundary so block siblings render as
    # separate lines.
    if element.text:
        out.append(element.text)
    if is_block:
        # Flush whatever accumulated *before* the block opens onto its
        # own line by inserting an explicit newline marker. We collapse
        # consecutive newlines later in `_normalise_whitespace`.
        out.append("\n")
    for child in element:
        _walk(child, out)
    if is_block:
        out.append("\n")
    if element.tail:
        out.append(element.tail)


def _local_name(tag: str) -> str:
    """Strip the `{ns}` prefix ElementTree adds to namespaced tags.

    `<ac:structured-macro>` parses as `{http://atlassian.com/content}structured-macro`;
    we want `structured-macro` for the BLOCK_TAGS lookup so namespace
    drift (Atlassian has reissued the URI at least once) doesn't break
    the walker.
    """
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


_NEWLINE_RUN_RE = re.compile(r"\n[\s\n]*\n")
_INLINE_WHITESPACE_RE = re.compile(r"[ \t]+")
_TAG_RE = re.compile(r"<[^>]+>")


def _normalise_whitespace(text: str) -> str:
    """Collapse runs of newlines + intra-line whitespace.

    Storage XHTML often puts block tags on their own lines, which the
    walker turns into `\n` + indent + `\n` + ... — joining several
    blank "paragraphs" between real content. We collapse any run of
    whitespace that contains at least one newline into exactly two
    newlines, and any inline run of spaces/tabs into one space.
    """
    collapsed = _NEWLINE_RUN_RE.sub("\n\n", text)
    collapsed = _INLINE_WHITESPACE_RE.sub(" ", collapsed)
    return collapsed.strip()


def _tag_strip(body: str) -> str:
    """Last-resort: strip every tag regex-style, decode `&nbsp;`.

    Used only when `xml.etree.ElementTree` refuses to parse the body.
    Loses macro structure (rich-text inside a panel macro is just text
    by the time we get here) but preserves every printable character
    the operator typed.
    """
    text = body
    for needle, replacement in _ENTITY_FIXUPS:
        text = text.replace(needle, replacement)
    text = _TAG_RE.sub(" ", text)
    return _normalise_whitespace(text)


__all__ = ["storage_to_text"]
