"""`pleno-pii-scanner connectors` — list / describe registered SourceConnectors.

Read-only inspection of the connector registry. Useful for operators
auditing which third-party wheels are installed and which capabilities
each kind advertises before they wire it into a scan plan.
"""

from __future__ import annotations

import json

import click

from pleno_pii_scanner.sources.registry import (
    UnknownConnectorError,
    get,
    list_specs,
)


@click.group(name="connectors")
def connectors_group() -> None:
    """Inspect installed SourceConnectors."""


@connectors_group.command(name="list")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
)
def cmd_list(fmt: str) -> None:
    """List every registered connector kind."""
    specs = list_specs()
    if fmt == "json":
        payload = [
            {
                "kind": s.kind,
                "version": s.version,
                "description": s.description,
                "incremental": s.capabilities.incremental,
                "binary": s.capabilities.binary,
                "max_concurrent_fetches": s.capabilities.max_concurrent_fetches,
                "required_scopes": list(s.required_scopes),
            }
            for s in specs
        ]
        click.echo(json.dumps(payload, indent=2))
        return
    if not specs:
        click.echo("(no connectors registered)", err=True)
        return
    width = max(len(s.kind) for s in specs)
    for s in specs:
        click.echo(f"{s.kind:<{width}}  {s.version:<8}  {s.description}")


@connectors_group.command(name="describe")
@click.argument("kind")
def cmd_describe(kind: str) -> None:
    """Print a connector's full spec as JSON."""
    try:
        spec = get(kind)
    except UnknownConnectorError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(
        json.dumps(
            {
                "kind": spec.kind,
                "version": spec.version,
                "description": spec.description,
                "required_scopes": list(spec.required_scopes),
                "capabilities": {
                    "incremental": spec.capabilities.incremental,
                    "binary": spec.capabilities.binary,
                    "content_hash_delta": spec.capabilities.content_hash_delta,
                    "max_concurrent_fetches": (
                        spec.capabilities.max_concurrent_fetches
                    ),
                    "streaming": spec.capabilities.streaming,
                },
                "config_schema": (
                    dict(spec.config_schema) if spec.config_schema is not None else None
                ),
            },
            indent=2,
        )
    )


__all__ = ["connectors_group"]
