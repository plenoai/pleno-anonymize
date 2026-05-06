"""Slack file attachment helpers.

When a `conversations.history` page contains messages with a `files: [...]`
array, the scanner needs the file payload — not the message text alone.
Slack file URLs require the same Bearer token used for the Web API; the
SDK has a `files_completeUploadExternal` helper for uploads but no
streaming download primitive, so we hit `url_private_download` directly
with httpx.

Two extraction paths:

  * text-like (mimetype starts with `text/`, or extension matches a small
    code-like allowlist) → return Document with `text=` decoded as UTF-8
    with errors='replace' to mirror the dir connector
  * everything else → return Document with `binary=` and let the
    ContentExtractor registry (#8) decide how to scan it
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

# Mimetypes for extensions that don't always carry a text/* mimetype but
# whose contents are unambiguously source code. Keep this short — the
# ContentExtractor registry handles the heavy lifting; this list only
# decides whether to send the bytes through the text path or the binary
# path at the connector boundary.
_TEXT_LIKE_EXTENSIONS: frozenset[str] = frozenset(
    {
        "py",
        "pyi",
        "js",
        "ts",
        "tsx",
        "jsx",
        "rb",
        "go",
        "rs",
        "java",
        "kt",
        "swift",
        "c",
        "cc",
        "cpp",
        "h",
        "hpp",
        "cs",
        "php",
        "scala",
        "sh",
        "bash",
        "zsh",
        "ps1",
        "lua",
        "r",
        "sql",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "conf",
        "json",
        "ndjson",
        "jsonl",
        "xml",
        "html",
        "htm",
        "css",
        "scss",
        "less",
        "md",
        "rst",
        "tex",
        "csv",
        "tsv",
        "log",
        "env",
        "dockerfile",
    }
)


@dataclass(frozen=True, slots=True)
class FileBody:
    """Outcome of `download_file` — either text or binary, never both."""

    text: str | None = None
    binary: bytes | None = None
    content_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        if (self.text is None) == (self.binary is None):
            raise ValueError("FileBody must populate exactly one of text or binary")


def is_text_like(mimetype: str | None, name: str | None) -> bool:
    """Heuristic: does the file want the text-decoding path?

    Mimetype takes precedence (text/*, application/json, application/xml,
    application/x-yaml). For files Slack returns without a useful
    mimetype (raw uploads, snippets), we fall back to the extension.
    """
    if mimetype:
        lowered = mimetype.lower()
        if lowered.startswith("text/"):
            return True
        if lowered in {
            "application/json",
            "application/xml",
            "application/x-yaml",
            "application/yaml",
            "application/x-sh",
        }:
            return True
    if name:
        # Use rsplit so files with multiple dots ("schema.gen.py") still
        # land on the final extension. Normalize case so .PY is treated
        # like .py.
        parts = name.rsplit(".", 1)
        if len(parts) == 2:
            ext = parts[1].lower()
            if ext in _TEXT_LIKE_EXTENSIONS:
                return True
        # Slack's `Dockerfile` snippet has no extension; match by lower
        # name as a small special case so common configurations don't
        # silently fall through to the binary path.
        if name.lower() in _TEXT_LIKE_EXTENSIONS:
            return True
    return False


async def download_file(
    *,
    client: httpx.AsyncClient,
    token: str,
    url: str,
    mimetype: str | None,
    name: str | None,
) -> FileBody:
    """Fetch `url` with Bearer auth and decide text vs binary.

    Caller owns the httpx client lifecycle — we don't open a fresh
    connection per file because the connector scans many files in
    sequence and connection reuse halves the wall time on warm caches.
    """
    response = await client.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=True,
    )
    response.raise_for_status()
    content_type = response.headers.get(
        "Content-Type", mimetype or "application/octet-stream"
    )
    raw = response.content
    if is_text_like(mimetype, name) or content_type.lower().startswith("text/"):
        # WHY errors='replace': Slack file uploads occasionally include a
        # spurious BOM or mixed encodings (Notepad/Windows users). The
        # core extractor will re-detect; we just need a string out so the
        # Document invariant holds.
        return FileBody(
            text=raw.decode("utf-8", errors="replace"),
            content_type=content_type,
        )
    return FileBody(binary=raw, content_type=content_type)


__all__ = ["FileBody", "download_file", "is_text_like"]
