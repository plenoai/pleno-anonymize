"""`pleno-pii-scanner scan` — unified single-source scan via the registry.

`scan run <kind>` looks up `kind` in the connector registry, builds the
connector via the registered factory, and drives it through the
IncrementalRunner. The detector pipeline (regex + NER + verify) lives
behind the runner so unchanged sub-sources / documents replay cached
findings instead of re-detecting.

Findings stream to stdout (one JSON object per finding) — the legacy
`pleno-pii-scanner dir / git-history / github` paths still own the
ScanStats + render_human / render_sarif emitters; piping the new CLI's
JSON through `jq` is the recommended bridge until those reporters
adopt the SourceConnector flow.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import click

from pleno_pii_scanner.detector import (
    DetectorFn,
    decode_findings,
    make_detector,
    schema_components,
)
from pleno_pii_scanner.scheduler import (
    GlobalRateLimiter,
    IncrementalRunner,
    Scheduler,
    SchedulerConfig,
    SourcePlan,
)
from pleno_pii_scanner.sources.base import (
    Document,
    DocumentChunk,
    DocumentRef,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import (
    UnknownConnectorError,
    create,
    list_kinds,
)
from pleno_pii_scanner.state import (
    SqliteScanCache,
    default_cache_path,
    schema_version,
)


@click.group(name="scan")
def scan_group() -> None:
    """Drive a SourceConnector via the unified scheduler."""


@scan_group.command(name="run")
@click.argument("kind")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="TOML file with the connector's per-source config.",
)
@click.option(
    "--config-json",
    "config_json",
    default=None,
    help="Inline JSON config; overrides --config.",
)
@click.option(
    "--include",
    multiple=True,
    help="Glob added to the SourceFilter include list.",
)
@click.option(
    "--exclude",
    multiple=True,
    help="Glob added to the SourceFilter exclude list.",
)
@click.option(
    "--max-size",
    type=int,
    default=None,
    help="Per-document size cap (bytes) applied at discover time.",
)
@click.option(
    "--scan-id",
    default="adhoc",
    show_default=True,
    help="Identifier the FindingsStore (when wired) attributes findings to.",
)
@click.option(
    "--report-format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Format of the per-scan summary printed to stdout.",
)
@click.option(
    "--cache/--no-cache",
    "use_cache",
    default=True,
    show_default=True,
    help=(
        "Enable the incremental scan cache. Sub-source level skip "
        "(unchanged repos / channels / drives) plus document-level skip "
        "(unchanged file payloads) reuse prior findings instead of "
        "re-running the detector pipeline. Disable when investigating "
        "false negatives or after a recognizer update."
    ),
)
@click.option(
    "--cache-path",
    "cache_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Override the SQLite cache file path. Default lives under "
        "$XDG_STATE_HOME/pleno/cache/scan_cache.sqlite and is shared "
        "across scan_ids."
    ),
)
@click.option(
    "--language",
    default="ja",
    show_default=True,
    help="NER language code passed to ner_pass.scan_text.",
)
@click.option(
    "--entities",
    "entities_csv",
    default=None,
    help=(
        "Comma-separated entity filter (e.g. 'PHONE_NUMBER,EMAIL'). "
        "Defaults to the full ja recognizer pack minus noisy entities."
    ),
)
@click.option(
    "--no-ner/--ner",
    "skip_ner",
    default=False,
    show_default=True,
    help=(
        "Skip the NER pass. ~50× faster on text-heavy scans but loses "
        "PERSON / ADDRESS detections that have no regex form."
    ),
)
@click.option(
    "--findings-out",
    "findings_out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Write findings to this file as one JSON object per line "
        "(JSONL). Defaults to stdout when omitted."
    ),
)
def cmd_run(
    kind: str,
    config_path: Path | None,
    config_json: str | None,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    max_size: int | None,
    scan_id: str,
    report_format: str,
    use_cache: bool,
    cache_path: Path | None,
    language: str,
    entities_csv: str | None,
    skip_ner: bool,
    findings_out: Path | None,
) -> None:
    """Run a single-source scan via the registered connector for KIND.

    Default behaviour: regex + NER + verify pipeline, sub-source +
    document level cache, findings streamed as JSONL to stdout (or
    `--findings-out`).
    """
    config = _load_config(config_path, config_json)
    try:
        connector = create(kind, config)
    except UnknownConnectorError as exc:
        raise click.ClickException(str(exc)) from None
    sf = SourceFilter(
        include=tuple(include),
        exclude=tuple(exclude),
        max_size=max_size,
    )
    summary = asyncio.run(
        _drive_scan(
            connector=connector,
            sf=sf,
            scan_id=scan_id,
            use_cache=use_cache,
            cache_path=cache_path,
            language=language,
            entities_csv=entities_csv,
            skip_ner=skip_ner,
            findings_out=findings_out,
        )
    )
    if report_format == "json":
        click.echo(json.dumps(summary, indent=2))
    else:
        cache = summary.get("cache")
        cache_blurb = ""
        if cache is not None:
            cache_blurb = (
                f" cache=sub({cache['subsource_hits']}/"
                f"{cache['subsource_total']})/"
                f"doc({cache['document_hits']}/"
                f"{cache['document_total']})"
            )
        click.echo(
            f"scan {kind}: refs_seen={summary['refs_seen']} "
            f"docs_fetched={summary['docs_fetched']} "
            f"findings_emitted={summary['findings_emitted']} "
            f"error={summary['error']}{cache_blurb}",
            err=True,
        )
    if summary["error"]:
        sys.exit(2)


@scan_group.command(name="kinds")
def cmd_kinds() -> None:
    """List every connector kind the unified `scan` accepts."""
    kinds = list_kinds()
    if not kinds:
        click.echo("(no connectors registered)", err=True)
        return
    for k in kinds:
        click.echo(k)


# --- helpers ---------------------------------------------------------


def _load_config(path: Path | None, inline_json: str | None) -> dict[str, Any]:
    if inline_json is not None:
        try:
            data = json.loads(inline_json)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"invalid --config-json: {exc}") from None
        if not isinstance(data, dict):
            raise click.ClickException("--config-json must decode to an object")
        return data
    if path is None:
        return {}
    with path.open("rb") as fh:
        # tomllib.load always returns the top-level table as a dict; a
        # syntactically invalid file raises TOMLDecodeError, which we
        # surface as a ClickException so operators see a clean error.
        try:
            return tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise click.ClickException(f"{path} is not valid TOML: {exc}") from None


def _resolve_recognizers(
    entities_csv: str | None,
) -> tuple[Any, tuple[str, ...] | None]:
    """Mirror cli._select_recognizers without inheriting its flags.

    Returns `(recognizers, ner_entity_filter)`. The NER filter is None
    when the operator did not constrain entities — that lets ner_pass
    return everything its model knows about. Heavy import is local so
    `--help` and `kinds` stay fast.
    """
    from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

    # Mirror cli._NOISY_ENTITIES; defined here to avoid pulling in the
    # legacy CLI module (which has a heavier import graph).
    noisy = {"DATE_OF_BIRTH"}
    if not entities_csv:
        return (
            tuple(r for r in ALL_JA_RECOGNIZERS if r.entity not in noisy),
            None,
        )
    if entities_csv.strip().upper() == "ALL":
        return (ALL_JA_RECOGNIZERS, None)
    wanted = tuple(e.strip() for e in entities_csv.split(",") if e.strip())
    selected = tuple(r for r in ALL_JA_RECOGNIZERS if r.entity in wanted)
    if not selected:
        # NER-only entity selection (PERSON / ADDRESS / ...): give the
        # verifier the full pack so it can attach context-keyword
        # boosts even though the regex pass has no patterns to fire.
        return (ALL_JA_RECOGNIZERS, wanted)
    return (selected, wanted)


def _open_findings_writer(path: Path | None):
    """Return (writer-callable, close-callable) for JSONL emission."""
    if path is None:
        out = sys.stdout

        def write(line: str) -> None:
            out.write(line)
            out.write("\n")

        def close() -> None:
            out.flush()

        return write, close

    fh = path.open("w", encoding="utf-8")

    def write(line: str) -> None:
        fh.write(line)
        fh.write("\n")

    def close() -> None:
        fh.close()

    return write, close


async def _drive_scan(
    *,
    connector: Any,
    sf: SourceFilter,
    scan_id: str,
    use_cache: bool,
    cache_path: Path | None,
    language: str,
    entities_csv: str | None,
    skip_ner: bool,
    findings_out: Path | None,
) -> dict[str, Any]:
    """Wire the detector + runner + cache for one SourcePlan."""
    recognizers, entity_filter = _resolve_recognizers(entities_csv)
    detector: DetectorFn = make_detector(
        recognizers,
        language=language,
        entities=entity_filter,
        skip_ner=skip_ner,
    )

    write_finding, close_findings = _open_findings_writer(findings_out)

    async def emit_findings(
        source_id: str,
        sub_id: str | None,
        count: int,
        payload: bytes,
        replayed: bool,
    ) -> None:
        if count == 0:
            return
        for f in decode_findings(payload):
            write_finding(
                json.dumps(
                    {
                        "source_id": source_id,
                        "sub_id": sub_id,
                        "replayed": replayed,
                        "entity": f.entity,
                        "file": f.file,
                        "line": f.line,
                        "col": f.col,
                        "score": f.score,
                        "snippet": f.snippet,
                        "matched": f.matched,
                        "pattern_name": f.pattern_name,
                        "verification": f.verification,
                        "fingerprint": f.fingerprint(),
                    },
                    ensure_ascii=False,
                )
            )

    plan = SourcePlan(connector=connector, filter=sf)
    rl = GlobalRateLimiter()
    sch = Scheduler(config=SchedulerConfig(), rate_limiter=rl)

    try:
        if not use_cache:
            results = await sch.run(
                [plan],
                scan_id=scan_id,
                scan_fn=_make_uncached_scan_fn(detector, emit_findings),
            )
            return _summary_from_result(results[0])

        cache = await SqliteScanCache.open(path=cache_path)
        try:
            runner = IncrementalRunner(
                sch,
                cache,
                # Components captured: detector wire/logic versions,
                # recognizer pack content fingerprint, and the operator
                # flags that change emitted findings. Notably absent:
                # the pleno-pii-scanner package version — a patch
                # release that doesn't touch detector logic must not
                # blow away the cache.
                schema_version=schema_version(
                    *schema_components(
                        recognizers,
                        language=language,
                        entities=entity_filter,
                        skip_ner=skip_ner,
                    )
                ),
            )
            inc_results = await runner.run(
                [plan],
                scan_id=scan_id,
                detector=detector,
                on_findings=emit_findings,
            )
        finally:
            await cache.close()
        inc = inc_results[0]
        summary = _summary_from_result(inc.source_result)
        summary["cache"] = {
            "subsource_total": inc.cache_stats.subsource_total,
            "subsource_hits": inc.cache_stats.subsource_hits,
            "subsource_misses": inc.cache_stats.subsource_misses,
            "document_total": inc.cache_stats.document_total,
            "document_hits": inc.cache_stats.document_hits,
            "document_misses": inc.cache_stats.document_misses,
            "path": str(cache_path or default_cache_path()),
        }
        return summary
    finally:
        await sch.close()
        close_findings()


def _make_uncached_scan_fn(detector: DetectorFn, emit_findings):
    """Adapt a `DetectorFn` to the Scheduler's plain `scan_fn` signature
    when caching is disabled. Findings still flow through `emit_findings`
    so `--no-cache` and the cached path produce byte-identical output
    streams.
    """

    async def scan_fn(ref: DocumentRef, doc: Document | DocumentChunk) -> int:
        count, payload = await detector(ref, doc)
        await emit_findings(ref.source_id, _sub_id(ref), count, payload, False)
        return count

    return scan_fn


def _sub_id(ref: DocumentRef) -> str | None:
    from pleno_pii_scanner.sources.base import SUBSOURCE_METADATA_KEY

    return ref.metadata.get(SUBSOURCE_METADATA_KEY)


def _summary_from_result(r: Any) -> dict[str, Any]:
    return {
        "source_id": r.source_id,
        "source_kind": r.source_kind,
        "refs_seen": r.refs_seen,
        "docs_fetched": r.docs_fetched,
        "findings_emitted": r.findings_emitted,
        "started_at": r.started_at.isoformat(),
        "completed_at": r.completed_at.isoformat(),
        "error": r.error,
    }


__all__ = ["scan_group"]
