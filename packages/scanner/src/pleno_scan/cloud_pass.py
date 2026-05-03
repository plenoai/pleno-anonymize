"""Cloud-mode scan: delegate analysis to a pleno-anonymize HTTP API.

Used when --base-url is supplied. Sends each file (chunked under the
server's 100k-char limit) to POST /api/analyze and translates the
returned offsets back into Finding(line, col).

Cloud mode benefits:
- Includes NER entities (PERSON, ADDRESS, ORGANIZATION) the local
  regex pass cannot see.
- Presidio's contextual scoring is applied server-side.

Cloud mode costs:
- Network latency. Use --concurrency to parallelize.
- Server's 100k-char request cap requires chunking large files.
"""

from __future__ import annotations

import asyncio
import bisect
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx

from pleno_scan.models import Finding


# Server caps text at 100_000 chars (server.AnalyzeRequest). Leave headroom.
_CHUNK_LIMIT = 90_000


@dataclass(frozen=True, slots=True)
class CloudConfig:
    base_url: str
    language: str = "ja"
    api_key: str | None = None
    concurrency: int = 8
    timeout_s: float = 120.0
    entities: tuple[str, ...] | None = None


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    pos = 0
    while True:
        idx = text.find("\n", pos)
        if idx == -1:
            break
        offsets.append(idx + 1)
        pos = idx + 1
    return offsets


def _line_col(line_starts: list[int], offset: int) -> tuple[int, int]:
    line_idx = bisect.bisect_right(line_starts, offset) - 1
    return line_idx + 1, offset - line_starts[line_idx] + 1


def _chunk(text: str, limit: int = _CHUNK_LIMIT) -> Iterable[tuple[int, str]]:
    """Yield (offset_in_full_text, chunk_text). Splits on newlines when possible."""
    if len(text) <= limit:
        yield 0, text
        return
    pos = 0
    n = len(text)
    while pos < n:
        end = min(pos + limit, n)
        if end < n:
            nl = text.rfind("\n", pos, end)
            if nl > pos:
                end = nl + 1
        yield pos, text[pos:end]
        pos = end


async def _analyze(
    client: httpx.AsyncClient, cfg: CloudConfig, text: str
) -> list[dict]:
    headers: dict[str, str] = {"content-type": "application/json"}
    if cfg.api_key:
        headers["authorization"] = f"Bearer {cfg.api_key}"
    body: dict = {"text": text, "language": cfg.language}
    if cfg.entities:
        body["entities"] = list(cfg.entities)
    url = cfg.base_url.rstrip("/") + "/api/analyze"
    r = await client.post(url, json=body, headers=headers)
    r.raise_for_status()
    return r.json()


async def _scan_one(
    client: httpx.AsyncClient,
    cfg: CloudConfig,
    file: str,
    text: str,
) -> list[Finding]:
    if not text:
        return []
    line_starts = _line_offsets(text)
    findings: list[Finding] = []
    for offset, chunk in _chunk(text):
        try:
            results = await _analyze(client, cfg, chunk)
        except (httpx.HTTPError, ValueError):
            continue
        for r in results:
            start = offset + int(r["start"])
            end = offset + int(r["end"])
            line, col = _line_col(line_starts, start)
            line_end_idx = bisect.bisect_right(line_starts, start)
            line_end = (
                line_starts[line_end_idx]
                if line_end_idx < len(line_starts)
                else len(text)
            )
            snippet = text[line_starts[line - 1] : line_end].rstrip("\n")
            if len(snippet) > 240:
                rel = start - line_starts[line - 1]
                snippet = snippet[max(0, rel - 80) : rel + 160]
            findings.append(
                Finding(
                    entity=str(r["entity_type"]),
                    file=file,
                    line=line,
                    col=col,
                    score=float(r["score"]),
                    snippet=snippet,
                    matched=text[start:end],
                    pattern_name="cloud",
                )
            )
    return findings


async def _scan_files_cloud_async(
    files: list[tuple[Path, Path]],
    file_text: dict[str, str],
    cfg: CloudConfig,
) -> list[Finding]:
    sem = asyncio.Semaphore(cfg.concurrency)
    timeout = httpx.Timeout(cfg.timeout_s)
    findings: list[Finding] = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        async def _task(rel_str: str) -> list[Finding]:
            async with sem:
                return await _scan_one(client, cfg, rel_str, file_text[rel_str])

        results = await asyncio.gather(
            *[_task(rel.as_posix()) for rel, _ in files], return_exceptions=False
        )

    for r in results:
        findings.extend(r)
    return findings


def scan_files_cloud(
    files: list[tuple[Path, Path]],
    file_text: dict[str, str],
    cfg: CloudConfig,
) -> list[Finding]:
    """Synchronous entry point — runs the async scanner via asyncio.run."""
    if not files:
        return []
    return asyncio.run(_scan_files_cloud_async(files, file_text, cfg))
