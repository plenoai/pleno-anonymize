"""`pleno-pii-scanner schedule` — manage the ScheduleRegistry (#11, ADR §12).

Persists schedules in the SqliteScheduleStore at the default
$XDG_STATE_HOME location. The `run` subcommand starts the cron loop and
blocks until SIGINT — operators typically run it under systemd / a
container restart policy.

The `add` and `run` subcommands accept a `--plan-ref` string that the
RunFn translates into a real scan. For now `--plan-ref` is opaque: a
follow-up PR (CLI scan + plan loader) wires plan_refs back into actual
SourcePlan execution. Today, `schedule run` invokes a stub RunFn that
prints the plan_ref it would have run; this lets operators verify their
cron expressions are correct without standing up the full scheduler.
"""

from __future__ import annotations

import asyncio
import json
import signal
from pathlib import Path

import click

from pleno_pii_scanner.schedule import (
    CronExpression,
    Schedule,
    ScheduleOutcome,
    ScheduleRegistry,
    SqliteScheduleStore,
)


@click.group(name="schedule")
@click.option(
    "--registry-path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override the default SqliteScheduleStore path "
    "($XDG_STATE_HOME/pleno/schedule/registry.sqlite).",
)
@click.pass_context
def schedule_group(ctx: click.Context, registry_path: Path | None) -> None:
    """Manage cron-driven scans."""
    ctx.ensure_object(dict)
    ctx.obj["registry_path"] = registry_path


@schedule_group.command(name="add")
@click.option("--id", "schedule_id", required=True, help="Stable identifier.")
@click.option("--cron", "cron_expr", required=True, help="5-field cron or @hourly/@daily/...")
@click.option("--plan-ref", required=True, help="Opaque reference passed to RunFn.")
@click.option(
    "--jitter-seconds",
    type=int,
    default=0,
    show_default=True,
    help="Random delay window applied after cron fire (avoids thundering herds).",
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Repeatable tag attached to the schedule (e.g. team:sec).",
)
@click.option(
    "--disabled",
    is_flag=True,
    help="Register the schedule but leave it disabled (won't fire on tick).",
)
@click.pass_context
def cmd_add(
    ctx: click.Context,
    schedule_id: str,
    cron_expr: str,
    plan_ref: str,
    jitter_seconds: int,
    tags: tuple[str, ...],
    disabled: bool,
) -> None:
    """Register a new recurring schedule."""
    try:
        cron = CronExpression.parse(cron_expr)
    except ValueError as exc:
        raise click.ClickException(f"invalid cron: {exc}") from None
    sched = Schedule(
        id=schedule_id,
        cron=cron,
        plan_ref=plan_ref,
        jitter_seconds=jitter_seconds,
        enabled=not disabled,
        tags=tuple(tags),
    )
    asyncio.run(_add(ctx.obj["registry_path"], sched))
    click.echo(f"registered {schedule_id} (next: {sched.cron.expr})")


@schedule_group.command(name="list")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
)
@click.pass_context
def cmd_list(ctx: click.Context, fmt: str) -> None:
    """List every registered schedule."""
    items = asyncio.run(_list(ctx.obj["registry_path"]))
    if fmt == "json":
        click.echo(
            json.dumps(
                [
                    {
                        "id": s.id,
                        "cron": s.cron.expr,
                        "plan_ref": s.plan_ref,
                        "enabled": s.enabled,
                        "jitter_seconds": s.jitter_seconds,
                        "tags": list(s.tags),
                        "next_run_at": s.next_run_at.isoformat()
                        if s.next_run_at
                        else None,
                        "last_run_at": s.last_run_at.isoformat()
                        if s.last_run_at
                        else None,
                        "last_outcome": s.last_outcome.value
                        if s.last_outcome
                        else None,
                    }
                    for s in items
                ],
                indent=2,
            )
        )
        return
    if not items:
        click.echo("(no schedules registered)", err=True)
        return
    for s in items:
        flag = "ON " if s.enabled else "OFF"
        nxt = s.next_run_at.isoformat() if s.next_run_at else "—"
        click.echo(
            f"{flag}  {s.id:<24}  {s.cron.expr:<20}  next={nxt}  plan={s.plan_ref}"
        )


@schedule_group.command(name="remove")
@click.argument("schedule_id")
@click.pass_context
def cmd_remove(ctx: click.Context, schedule_id: str) -> None:
    """Unregister a schedule."""
    asyncio.run(_remove(ctx.obj["registry_path"], schedule_id))
    click.echo(f"removed {schedule_id}")


@schedule_group.command(name="run")
@click.option(
    "--interval",
    type=float,
    default=30.0,
    show_default=True,
    help="Seconds between tick checks.",
)
@click.option(
    "--once",
    is_flag=True,
    help="Run a single tick and exit (useful for cron-of-cron deployments).",
)
@click.pass_context
def cmd_run(ctx: click.Context, interval: float, once: bool) -> None:
    """Start the scheduler loop (blocks until SIGINT) or run one tick."""
    asyncio.run(_run(ctx.obj["registry_path"], interval=interval, once=once))


# --- helpers -----------------------------------------------------------


async def _open_store(path: Path | None) -> SqliteScheduleStore:
    return await SqliteScheduleStore.open(path=path)


async def _add(path: Path | None, schedule: Schedule) -> None:
    store = await _open_store(path)
    try:
        # Use a no-op run_fn — `add` only persists; `run` is what fires.
        reg = ScheduleRegistry(store, _stub_run_fn)
        await reg.register(schedule)
    finally:
        await store.close()


async def _list(path: Path | None) -> list[Schedule]:
    store = await _open_store(path)
    try:
        reg = ScheduleRegistry(store, _stub_run_fn)
        return await reg.list_schedules()
    finally:
        await store.close()


async def _remove(path: Path | None, schedule_id: str) -> None:
    store = await _open_store(path)
    try:
        reg = ScheduleRegistry(store, _stub_run_fn)
        await reg.unregister(schedule_id)
    finally:
        await store.close()


async def _run(path: Path | None, *, interval: float, once: bool) -> None:
    store = await _open_store(path)
    try:
        reg = ScheduleRegistry(store, _print_run_fn)
        if once:
            tasks = await reg.tick()
            if tasks:
                await asyncio.gather(*tasks)
            return
        cancel = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _request_stop() -> None:
            click.echo("schedule: SIGINT received, draining…", err=True)
            cancel.set()

        # WHY: signal handlers via add_signal_handler are the asyncio-native
        # way to interrupt run_forever; KeyboardInterrupt would surface from
        # asyncio.run but we want graceful drain instead of a stack trace.
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_stop)
            except NotImplementedError:
                # Windows ProactorEventLoop doesn't support add_signal_handler.
                # Fall back to letting KeyboardInterrupt propagate from cancel().
                pass
        await reg.run_forever(interval=interval, cancel=cancel)
    finally:
        await store.close()


async def _stub_run_fn(_sched: Schedule) -> ScheduleOutcome:
    """Used by add/list/remove which never actually fire schedules."""
    return ScheduleOutcome.SKIPPED


async def _print_run_fn(sched: Schedule) -> ScheduleOutcome:
    """Used by `run` until the unified scan plan loader is wired in.

    Prints what the registry would have invoked. Operators use this to
    verify cron expressions and SLA hooks fire at the right times before
    pointing the RunFn at a real Scheduler.run_one path.
    """
    click.echo(
        f"schedule fire: id={sched.id} plan_ref={sched.plan_ref} "
        f"cron={sched.cron.expr} tags={list(sched.tags)}",
        err=True,
    )
    return ScheduleOutcome.SUCCESS


__all__ = ["schedule_group"]
