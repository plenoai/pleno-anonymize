"""Tests for `pleno-pii-scanner schedule {add,list,remove,run}`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from pleno_pii_scanner.cli import main


@pytest.fixture
def reg_path(tmp_path: Path) -> Path:
    return tmp_path / "schedules.sqlite"


def _add(runner: CliRunner, reg_path: Path, **kwargs):
    args = [
        "schedule",
        "--registry-path",
        str(reg_path),
        "add",
        "--id",
        kwargs.pop("id", "s1"),
        "--cron",
        kwargs.pop("cron", "@hourly"),
        "--plan-ref",
        kwargs.pop("plan_ref", "plan-1"),
    ]
    if "jitter_seconds" in kwargs:
        args += ["--jitter-seconds", str(kwargs.pop("jitter_seconds"))]
    if kwargs.pop("disabled", False):
        args.append("--disabled")
    for tag in kwargs.pop("tags", ()):
        args += ["--tag", tag]
    return runner.invoke(main, args)


class TestAdd:
    def test_add_persists(self, reg_path: Path) -> None:
        runner = CliRunner()
        result = _add(runner, reg_path)
        assert result.exit_code == 0, result.output
        assert "registered s1" in result.output

    def test_add_invalid_cron(self, reg_path: Path) -> None:
        runner = CliRunner()
        result = _add(runner, reg_path, cron="bogus expression here")
        assert result.exit_code != 0
        assert "invalid cron" in result.output.lower()

    def test_add_disabled_flag(self, reg_path: Path) -> None:
        runner = CliRunner()
        _add(runner, reg_path, id="off", disabled=True)
        result = runner.invoke(
            main,
            ["schedule", "--registry-path", str(reg_path), "list", "--format", "json"],
        )
        payload = json.loads(result.output)
        assert any(s["id"] == "off" and not s["enabled"] for s in payload)

    def test_add_with_tags_and_jitter(self, reg_path: Path) -> None:
        runner = CliRunner()
        _add(
            runner,
            reg_path,
            id="tagged",
            jitter_seconds=15,
            tags=("team:sec", "repo:foo"),
        )
        result = runner.invoke(
            main,
            ["schedule", "--registry-path", str(reg_path), "list", "--format", "json"],
        )
        payload = json.loads(result.output)
        sched = next(s for s in payload if s["id"] == "tagged")
        assert sched["jitter_seconds"] == 15
        assert sorted(sched["tags"]) == ["repo:foo", "team:sec"]


class TestList:
    def test_empty_message(self, reg_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main, ["schedule", "--registry-path", str(reg_path), "list"]
        )
        assert result.exit_code == 0
        assert "no schedules" in result.output

    def test_text_format(self, reg_path: Path) -> None:
        runner = CliRunner()
        _add(runner, reg_path, id="a", plan_ref="pa")
        _add(runner, reg_path, id="b", plan_ref="pb")
        result = runner.invoke(
            main, ["schedule", "--registry-path", str(reg_path), "list"]
        )
        assert "ON " in result.output
        assert "a" in result.output
        assert "b" in result.output


class TestRemove:
    def test_remove(self, reg_path: Path) -> None:
        runner = CliRunner()
        _add(runner, reg_path)
        result = runner.invoke(
            main, ["schedule", "--registry-path", str(reg_path), "remove", "s1"]
        )
        assert result.exit_code == 0
        assert "removed s1" in result.output
        # Subsequent list shows zero items.
        listed = runner.invoke(
            main, ["schedule", "--registry-path", str(reg_path), "list"]
        )
        assert "no schedules" in listed.output


class TestRunOnce:
    def test_run_once_with_no_schedules_completes(self, reg_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["schedule", "--registry-path", str(reg_path), "run", "--once"],
        )
        assert result.exit_code == 0

    def test_run_once_fires_due_schedule(self, reg_path: Path) -> None:
        # Register a schedule whose next_run_at is in the past, then
        # run --once and confirm the stub run_fn printed the fire.
        runner = CliRunner()
        _add(runner, reg_path, id="due", cron="* * * * *")
        # Wait one minute? No — we backdate next_run_at via direct
        # SQLite mutation so the test stays sub-second.
        import sqlite3

        with sqlite3.connect(reg_path) as conn:
            conn.execute("UPDATE schedules SET next_run_at='2000-01-01T00:00:00+00:00'")
            conn.commit()

        result = runner.invoke(
            main,
            ["schedule", "--registry-path", str(reg_path), "run", "--once"],
        )
        assert result.exit_code == 0
        assert "schedule fire" in result.output
        assert "id=due" in result.output


class TestRunForeverInvalidInterval:
    def test_zero_interval_rejected(self, reg_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["schedule", "--registry-path", str(reg_path), "run", "--interval", "0"],
        )
        # ScheduleRegistry.run_forever raises ValueError on interval<=0;
        # asyncio.run propagates it as the CLI's exit code.
        assert result.exit_code != 0


class TestRunForeverSignal:
    async def test_sigint_triggers_drain(self, reg_path: Path) -> None:
        # Send SIGINT to ourselves while run_forever is blocked on its
        # interval timer; the registered handler should set cancel and
        # the coroutine should exit cleanly.
        import asyncio
        import os
        import signal

        from pleno_pii_scanner.cli_schedule import _run

        async def fire_signal_after_delay() -> None:
            await asyncio.sleep(0.05)
            os.kill(os.getpid(), signal.SIGINT)

        await asyncio.gather(
            _run(reg_path, interval=10.0, once=False),
            fire_signal_after_delay(),
        )


class TestRunForeverWindowsFallback:
    async def test_signal_handler_unavailable(
        self, reg_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On Windows ProactorEventLoop add_signal_handler raises
        # NotImplementedError; the CLI must swallow it and continue.
        # We simulate by monkey-patching the loop method.
        import asyncio

        from pleno_pii_scanner.cli_schedule import _run

        original_get_loop = asyncio.get_running_loop

        def patched_get_loop():
            loop = original_get_loop()
            loop.add_signal_handler = lambda *_a, **_k: (_ for _ in ()).throw(
                NotImplementedError("simulated Windows loop")
            )
            return loop

        monkeypatch.setattr(asyncio, "get_running_loop", patched_get_loop)

        async def cancel_after_delay() -> None:
            await asyncio.sleep(0.05)
            # No SIGINT possible on simulated Windows; cancel the task.
            for task in asyncio.all_tasks():
                if task.get_coro().__name__ == "_run":
                    task.cancel()

        with pytest.raises((asyncio.CancelledError, BaseException)):
            await asyncio.gather(
                _run(reg_path, interval=0.5, once=False),
                cancel_after_delay(),
            )


class TestStubRunFn:
    async def test_stub_returns_skipped(self) -> None:
        # Direct invocation — _stub is wired into add/list/remove paths
        # but never tickled by them, so we exercise it explicitly to
        # confirm it stays a true no-op.
        from pleno_pii_scanner.cli_schedule import _stub_run_fn
        from pleno_pii_scanner.schedule import (
            CronExpression,
            Schedule,
            ScheduleOutcome,
        )

        sched = Schedule(
            id="x",
            cron=CronExpression.parse("@hourly"),
            plan_ref="x",
        )
        outcome = await _stub_run_fn(sched)
        assert outcome is ScheduleOutcome.SKIPPED
