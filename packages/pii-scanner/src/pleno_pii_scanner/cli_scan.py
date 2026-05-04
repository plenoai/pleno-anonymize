"""`pleno-pii-scanner scan` — unified single-source scan via the registry.

`scan <kind>` looks up `kind` in the connector registry, builds the
connector via the registered factory, and drives it through the
Scheduler. The result is a count of refs seen / docs fetched —
findings are printed to stdout when `--scan-fn=print-paths` (the
default) or piped to a real detector pipeline when wired in.

The full multi-source orchestration (`scan --plan plan.toml`) is
deliberately deferred to a follow-up PR — that path needs the
FindingsStore wiring + at least one enterprise connector landed before
it earns its complexity. For now `scan <kind>` is enough surface to
exercise every newly registered connector end-to-end.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import click

from pleno_pii_scanner.scheduler import (
    GlobalRateLimiter,
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
) -> None:
    """Run a single-source scan via the registered connector for KIND.

    Today emits per-document path/size summaries; the detector pipeline
    integration arrives in a follow-up PR. Verifies the connector
    contract (discover → fetch → close) end-to-end and lets operators
    confirm a config change still enumerates the expected refs.
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
    summary = asyncio.run(_drive_scan(connector, sf, scan_id))
    if report_format == "json":
        click.echo(json.dumps(summary, indent=2))
    else:
        click.echo(
            f"scan {kind}: refs_seen={summary['refs_seen']} "
            f"docs_fetched={summary['docs_fetched']} "
            f"findings_emitted={summary['findings_emitted']} "
            f"error={summary['error']}",
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


async def _drive_scan(
    connector: Any, sf: SourceFilter, scan_id: str
) -> dict[str, Any]:
    """Run one SourcePlan through the Scheduler and return its summary."""
    plan = SourcePlan(connector=connector, filter=sf)
    rl = GlobalRateLimiter()
    sch = Scheduler(config=SchedulerConfig(), rate_limiter=rl)
    try:
        results = await sch.run(
            [plan], scan_id=scan_id, scan_fn=_count_only_scan_fn
        )
    finally:
        await sch.close()
    [r] = results
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


async def _count_only_scan_fn(
    _ref: DocumentRef, _doc: Document | DocumentChunk
) -> int:
    """Stub scan_fn: count zero findings.

    The real wiring (regex_pass + ner_pass + verify) lives in the
    legacy `scan_directory` path. Bridging it through the unified scan
    is a follow-up — the abstraction is correct, just not connected
    yet, and a bridge that emits findings without going through the
    FindingsStore (which the legacy CLI does not use either) would be
    a transient hack we'd then have to undo.
    """
    return 0


__all__ = ["scan_group"]
