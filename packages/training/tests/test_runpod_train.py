"""Tests for issue #292: scripts/runpod_train.py.

`scripts/` is not an importable package (no __init__.py, not part of the
`pleno_ner_training` src layout), so the module under test is loaded by
file path rather than `import`.

Two things matter for this script and nothing else does, because the real
RunPod API is never called in CI:

1. `--dry-run` fully resolves the plan (pod spec / uploads / remote
   commands) and prints it without touching the network.
2. `delete_pod` always runs, even when a step after `create_pod` raises --
   this is the "no orphaned billing pod" guarantee from the issue.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

TRAINING_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = TRAINING_ROOT / "scripts" / "runpod_train.py"


def _load_runpod_train() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runpod_train", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses' `_is_type` resolves `list[str] | None`-style postponed
    # annotations via `sys.modules[cls.__module__]`, so the module must be
    # registered before exec_module runs the class bodies.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runpod_train = _load_runpod_train()


# ---------------------------------------------------------------------------
# --dry-run: plan resolution, no network calls
# ---------------------------------------------------------------------------


def test_dry_run_ja_prints_plan_without_touching_the_network(monkeypatch, capsys):
    class ExplodingClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("dry-run must not construct a RunPodClient")

    monkeypatch.setattr(runpod_train, "RunPodClient", ExplodingClient)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)

    exit_code = runpod_train.main(["--language", "ja", "--dry-run", "--pod-name", "test-ja-pod"])
    assert exit_code == 0

    plan = json.loads(capsys.readouterr().out)
    assert plan["pod_spec"]["name"] == "test-ja-pod"
    assert plan["pod_spec"]["computeType"] == "CPU"
    assert plan["pod_spec"]["cpuFlavorIds"] == ["cpu5c"]
    assert plan["pod_spec"]["vcpuCount"] == 8
    assert plan["uploads"] == ["configs/train_cnn.cfg", "data/processed/ja"]
    assert "spacy train train_cnn.cfg" in plan["remote_train_command"]
    assert "--paths.train ja/train.spacy" in plan["remote_train_command"]
    assert "--paths.dev ja/dev.spacy" in plan["remote_train_command"]
    assert plan["artifact_local_path"] == "output/model-best"
    # ja needs the sudachi-backed tokenizer wheel; en does not.
    assert "ja_core_news_sm" in plan["remote_setup_command"]


def test_dry_run_en_uses_en_language_defaults(capsys):
    exit_code = runpod_train.main(["--language", "en", "--dry-run", "--pod-name", "test-en-pod"])
    assert exit_code == 0

    plan = json.loads(capsys.readouterr().out)
    assert plan["uploads"] == ["configs/train_cnn_en.cfg", "data/processed/en"]
    assert "spacy train train_cnn_en.cfg" in plan["remote_train_command"]
    assert plan["artifact_local_path"] == "output/en/model-best"
    assert plan["remote_setup_command"] == "pip install spacy"


def test_dry_run_requires_no_api_key(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    # Would fail before reaching dry-run print if the env var were required.
    assert runpod_train.main(["--language", "ja", "--dry-run"]) == 0


def test_missing_api_key_fails_fast_without_dry_run(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    assert runpod_train.main(["--language", "ja"]) == 1


# ---------------------------------------------------------------------------
# Cleanup guarantee: delete_pod always runs, even on failure
# ---------------------------------------------------------------------------


class FakeRunPodClient:
    """Records calls; lets a test inject a failure at any step."""

    instances: list["FakeRunPodClient"] = []

    def __init__(self, api_key: str, base_url: str = "") -> None:
        self.api_key = api_key
        self.calls: list[tuple] = []
        self.deleted_pod_ids: list[str] = []
        FakeRunPodClient.instances.append(self)

    def create_pod(self, spec):
        self.calls.append(("create_pod", spec))
        return {"id": "pod-123"}

    def get_pod(self, pod_id: str):
        self.calls.append(("get_pod", pod_id))
        return {"publicIp": "10.0.0.1", "portMappings": {"22": 2222}}

    def delete_pod(self, pod_id: str) -> None:
        self.calls.append(("delete_pod", pod_id))
        self.deleted_pod_ids.append(pod_id)


@pytest.fixture(autouse=True)
def _reset_fake_client_registry():
    FakeRunPodClient.instances.clear()
    yield
    FakeRunPodClient.instances.clear()


def _plan_args(**overrides):
    args = runpod_train.build_arg_parser().parse_args(
        ["--language", "ja", "--pod-name", "test-pod"]
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return runpod_train.build_plan(args)


def test_delete_pod_runs_after_a_successful_training_run(monkeypatch):
    monkeypatch.setattr(runpod_train, "RunPodClient", FakeRunPodClient)
    monkeypatch.setattr(runpod_train, "upload_paths", lambda *a, **k: None)
    monkeypatch.setattr(runpod_train, "run_remote_command", lambda *a, **k: None)
    monkeypatch.setattr(runpod_train, "download_path", lambda *a, **k: None)

    plan = _plan_args()
    exit_code = runpod_train.run_training(plan, api_key="fake-key", api_base_url="https://example.invalid")

    assert exit_code == 0
    client = FakeRunPodClient.instances[0]
    assert client.deleted_pod_ids == ["pod-123"]


@pytest.mark.parametrize(
    "failing_step",
    ["upload_paths", "run_remote_command", "download_path"],
)
def test_delete_pod_still_runs_when_a_later_step_raises(monkeypatch, failing_step):
    monkeypatch.setattr(runpod_train, "RunPodClient", FakeRunPodClient)

    def _boom(*args, **kwargs):
        raise RuntimeError(f"simulated failure in {failing_step}")

    for step in ("upload_paths", "run_remote_command", "download_path"):
        monkeypatch.setattr(runpod_train, step, _boom if step == failing_step else (lambda *a, **k: None))

    plan = _plan_args()

    with pytest.raises(RuntimeError, match="simulated failure"):
        runpod_train.run_training(plan, api_key="fake-key", api_base_url="https://example.invalid")

    # The whole point of the try/finally: pod deletion is not optional.
    client = FakeRunPodClient.instances[0]
    assert client.deleted_pod_ids == ["pod-123"]


def test_delete_pod_still_runs_when_pod_never_boots(monkeypatch):
    monkeypatch.setattr(runpod_train, "RunPodClient", FakeRunPodClient)

    class NeverReadyClient(FakeRunPodClient):
        def get_pod(self, pod_id: str):
            self.calls.append(("get_pod", pod_id))
            return {"publicIp": None, "portMappings": {}}

    monkeypatch.setattr(runpod_train, "RunPodClient", NeverReadyClient)

    plan = _plan_args(boot_timeout_s=0)
    with pytest.raises(TimeoutError):
        runpod_train.run_training(plan, api_key="fake-key", api_base_url="https://example.invalid")

    client = NeverReadyClient.instances[0]
    assert client.deleted_pod_ids == ["pod-123"]
