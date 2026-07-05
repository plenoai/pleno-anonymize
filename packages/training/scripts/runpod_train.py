"""Single-command RunPod training orchestrator (issue #292).

Replaces the manual workflow in `docs/runpod-training.md` (console-click pod
deploy, hand-copied IP/PORT, manual scp, manual "Terminate Pod" click) with
one process that: create-pod -> wait for SSH -> upload data -> run the spaCy
training command -> scp the artifact back -> delete-pod. The delete always
runs (`try/finally`), so a crash mid-training cannot leave a pod billing
forever the way a forgotten manual Terminate click could.

Pod sizing mirrors the OOM findings in `docs/runpod-training.md` (CPU
CNN training needs >= 8 vCPU / 16 GB — smaller configs OOM and hang the
SSH daemon on real data volumes). The optional GPU path mirrors the
"Usage on RunPod" docstring in `train_supervised_300k_en.py` (transformer
training needs a GPU pod, not the CPU5 flavor used for CNN training).

RunPod REST API (https://rest.runpod.io/v1): POST/GET/DELETE /pods, using
the same field names (`imageName`, `computeType`, `cpuFlavorIds`,
`vcpuCount`, `gpuTypeIds`, `portMappings`, ...) as the `mcp__runpod__*`
tools, so a pod created here looks the same in the RunPod console.

Usage::

    # Plan only -- no network calls, no charges. Prints the pod spec,
    # the files that would be uploaded, and the exact remote commands.
    uv run python scripts/runpod_train.py --language ja --dry-run

    # Real run (creates a billable pod).
    RUNPOD_API_KEY=... uv run --extra training python scripts/runpod_train.py \\
        --language en \\
        --train-config configs/train_cnn_en.cfg \\
        --data-dir data/processed/en \\
        --local-output-dir output/en/model-best

Wired into `make runpod-train-ja` / `make runpod-train-en` (see Makefile).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

API_BASE_URL = "https://rest.runpod.io/v1"

# RunPod's official lightweight CPU-only template ("Runpod Ubuntu 20.04" in
# docs/runpod-training.md). Verify at https://console.runpod.io/deploy?type=CPU
# before relying on this tag long-term -- override with --image / RUNPOD_IMAGE
# if RunPod has retired it. This is the one place the image lives now,
# instead of being re-discovered by hand every run.
DEFAULT_CPU_IMAGE = "runpod/base:0.6.2-cpu"

# Per-language defaults lifted directly from the existing Makefile targets
# (train-cnn / train-en-cnn) and docs/runpod-training.md, so `make
# runpod-train-ja` / `make runpod-train-en` produce the same training run
# those local-only targets would have, just executed on RunPod instead.
LANGUAGE_DEFAULTS: dict[str, dict[str, Any]] = {
    "ja": {
        "train_config": "configs/train_cnn.cfg",
        "data_dir": "data/processed/ja",
        "remote_output_dir": "output",
        "local_output_dir": "output/model-best",
        # docs/runpod-training.md step 3: ja tokenization needs the ja
        # pipeline wheel (sudachi dict), not just bare spacy.
        "pip_packages": [
            "spacy",
            "ja_core_news_sm@https://github.com/explosion/spacy-models/releases/download/ja_core_news_sm-3.8.0/ja_core_news_sm-3.8.0-py3-none-any.whl",
        ],
    },
    "en": {
        "train_config": "configs/train_cnn_en.cfg",
        "data_dir": "data/processed/en",
        "remote_output_dir": "output/en",
        "local_output_dir": "output/en/model-best",
        "pip_packages": ["spacy"],
    },
}


@dataclass
class PodSpec:
    """Maps 1:1 onto the RunPod REST `POST /pods` request body."""

    name: str
    image: str
    compute_type: str  # "CPU" or "GPU"
    container_disk_gb: int
    volume_gb: int
    ports: list[str]
    cloud_type: str = "SECURE"
    cpu_flavor_ids: list[str] | None = None
    vcpu_count: int | None = None
    gpu_type_ids: list[str] | None = None
    gpu_count: int | None = None
    env: dict[str, str] = field(default_factory=dict)

    def to_api_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "imageName": self.image,
            "computeType": self.compute_type,
            "containerDiskInGb": self.container_disk_gb,
            "volumeInGb": self.volume_gb,
            "ports": self.ports,
            "cloudType": self.cloud_type,
            "env": self.env,
        }
        if self.compute_type == "CPU":
            payload["cpuFlavorIds"] = self.cpu_flavor_ids
            payload["vcpuCount"] = self.vcpu_count
        else:
            payload["gpuTypeIds"] = self.gpu_type_ids
            payload["gpuCount"] = self.gpu_count
        return payload


@dataclass
class TrainingPlan:
    """Everything `--dry-run` shows and the real run executes -- built once,
    by one function, so the two paths can never silently diverge."""

    pod_spec: PodSpec
    uploads: list[str]
    remote_workdir: str
    remote_setup_command: str | None
    remote_train_command: str
    artifact_remote_path: str
    artifact_local_path: str
    ssh_key: str
    boot_timeout_s: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "pod_spec": self.pod_spec.to_api_payload(),
            "uploads": self.uploads,
            "remote_workdir": self.remote_workdir,
            "remote_setup_command": self.remote_setup_command,
            "remote_train_command": self.remote_train_command,
            "artifact_remote_path": self.artifact_remote_path,
            "artifact_local_path": self.artifact_local_path,
            "ssh_key": self.ssh_key,
            "boot_timeout_s": self.boot_timeout_s,
        }


class RunPodClient:
    """Thin wrapper around the RunPod REST API. `requests` is imported lazily
    so `--dry-run` never needs it installed."""

    def __init__(self, api_key: str, base_url: str = API_BASE_URL, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def create_pod(self, spec: PodSpec) -> dict[str, Any]:
        import requests

        resp = requests.post(
            f"{self._base_url}/pods",
            headers=self._headers(),
            json=spec.to_api_payload(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_pod(self, pod_id: str) -> dict[str, Any]:
        import requests

        resp = requests.get(
            f"{self._base_url}/pods/{pod_id}",
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_pod(self, pod_id: str) -> None:
        import requests

        resp = requests.delete(
            f"{self._base_url}/pods/{pod_id}",
            headers=self._headers(),
            timeout=self._timeout,
        )
        resp.raise_for_status()


def wait_for_pod_ready(
    client: RunPodClient,
    pod_id: str,
    timeout_s: int = 300,
    poll_interval_s: float = 5.0,
) -> tuple[str, int]:
    """Poll GET /pods/{id} until SSH (port 22) has a public IP + mapped port."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pod = client.get_pod(pod_id)
        ip = pod.get("publicIp")
        ssh_port = (pod.get("portMappings") or {}).get("22")
        if ip and ssh_port:
            return ip, int(ssh_port)
        time.sleep(poll_interval_s)
    raise TimeoutError(f"pod {pod_id} did not expose SSH within {timeout_s}s")


def _ssh_base_args(host: str, port: int, ssh_key: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-p",
        str(port),
        "-i",
        ssh_key,
        f"root@{host}",
    ]


def run_remote_command(host: str, port: int, ssh_key: str, command: str) -> None:
    subprocess.run(_ssh_base_args(host, port, ssh_key) + [command], check=True)


def upload_paths(host: str, port: int, ssh_key: str, local_paths: list[str], remote_dir: str) -> None:
    for local_path in local_paths:
        subprocess.run(
            [
                "scp",
                "-o",
                "StrictHostKeyChecking=no",
                "-P",
                str(port),
                "-i",
                ssh_key,
                "-r",
                local_path,
                f"root@{host}:{remote_dir}/",
            ],
            check=True,
        )


def download_path(host: str, port: int, ssh_key: str, remote_path: str, local_path: str) -> None:
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-P",
            str(port),
            "-i",
            ssh_key,
            "-r",
            f"root@{host}:{remote_path}",
            local_path,
        ],
        check=True,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--language", choices=sorted(LANGUAGE_DEFAULTS), required=True)
    parser.add_argument("--train-config", help="spaCy train config, relative to packages/training/")
    parser.add_argument("--data-dir", help="dir containing train.spacy/dev.spacy")
    parser.add_argument("--remote-output-dir", help="output dir on the pod passed to `spacy train --output`")
    parser.add_argument("--local-output-dir", help="where model-best is saved locally after download")
    parser.add_argument("--pod-name", help="override the generated pod name")
    parser.add_argument("--compute-type", choices=["CPU", "GPU"], default="CPU")
    parser.add_argument(
        "--cpu-flavor-id",
        default="cpu5c",
        help="RunPod CPU flavor id. cpu5c = 'CPU5 Compute-Optimized' from docs/runpod-training.md",
    )
    parser.add_argument(
        "--vcpu-count",
        type=int,
        default=8,
        help="8 vCPU is the documented OOM-safe minimum for CNN training (docs/runpod-training.md)",
    )
    parser.add_argument("--gpu-type-id", action="append", dest="gpu_type_ids", help="repeatable; only used with --compute-type GPU")
    parser.add_argument("--gpu-count", type=int, default=1)
    parser.add_argument("--gpu-id", type=int, default=None, help="passed to `spacy train --gpu-id` when training on a GPU pod")
    parser.add_argument("--image", default=DEFAULT_CPU_IMAGE)
    parser.add_argument("--container-disk-gb", type=int, default=20)
    parser.add_argument("--volume-gb", type=int, default=5, help="ephemeral single-run pod: no persistent volume needed")
    parser.add_argument("--python-bin", default="python3.13", help="RunPod Ubuntu 20.04's `python3` is 3.8; use python3.13")
    parser.add_argument("--remote-workdir", default="/root")
    parser.add_argument("--ssh-key", default=os.path.expanduser("~/.ssh/id_ed25519"))
    parser.add_argument("--boot-timeout-s", type=int, default=300, help="max seconds to wait for the pod to expose SSH")
    parser.add_argument("--api-key", default=None, help="defaults to $RUNPOD_API_KEY")
    parser.add_argument("--api-base-url", default=API_BASE_URL)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan (pod spec, uploads, commands) as JSON and exit; makes no API calls",
    )
    return parser


def build_plan(args: argparse.Namespace) -> TrainingPlan:
    """Pure function -- resolves language defaults + CLI overrides into a
    concrete plan. No network/subprocess side effects, so both --dry-run and
    the real run go through this exact code path."""
    defaults = LANGUAGE_DEFAULTS[args.language]
    train_config = args.train_config or defaults["train_config"]
    data_dir = args.data_dir or defaults["data_dir"]
    remote_output_dir = args.remote_output_dir or defaults["remote_output_dir"]
    local_output_dir = args.local_output_dir or defaults["local_output_dir"]
    pip_packages: list[str] = defaults["pip_packages"]

    pod_name = args.pod_name or f"ner-train-{args.language}-{int(time.time())}"

    pod_spec = PodSpec(
        name=pod_name,
        image=args.image,
        compute_type=args.compute_type,
        container_disk_gb=args.container_disk_gb,
        volume_gb=args.volume_gb,
        ports=["22/tcp"],
        cpu_flavor_ids=[args.cpu_flavor_id] if args.compute_type == "CPU" else None,
        vcpu_count=args.vcpu_count if args.compute_type == "CPU" else None,
        gpu_type_ids=args.gpu_type_ids if args.compute_type == "GPU" else None,
        gpu_count=args.gpu_count if args.compute_type == "GPU" else None,
    )

    config_name = Path(train_config).name
    data_name = Path(data_dir).name
    train_cmd = (
        f"cd {args.remote_workdir} && {args.python_bin} -m spacy train {config_name} "
        f"--output {remote_output_dir} "
        f"--paths.train {data_name}/train.spacy "
        f"--paths.dev {data_name}/dev.spacy"
    )
    if args.compute_type == "GPU" and args.gpu_id is not None:
        train_cmd += f" --gpu-id {args.gpu_id}"

    setup_cmd = " && ".join(f"pip install {pkg}" for pkg in pip_packages) if pip_packages else None

    return TrainingPlan(
        pod_spec=pod_spec,
        uploads=[train_config, data_dir],
        remote_workdir=args.remote_workdir,
        remote_setup_command=setup_cmd,
        remote_train_command=train_cmd,
        artifact_remote_path=f"{args.remote_workdir}/{remote_output_dir}/model-best",
        artifact_local_path=local_output_dir,
        ssh_key=args.ssh_key,
        boot_timeout_s=args.boot_timeout_s,
    )


def run_training(plan: TrainingPlan, api_key: str, api_base_url: str) -> int:
    """Create the pod, run the plan, and always delete the pod -- success or
    failure. This is the guarantee issue #292 asks for: no forgotten
    Terminate click, no orphaned billing pod."""
    client = RunPodClient(api_key, base_url=api_base_url)
    pod = client.create_pod(plan.pod_spec)
    pod_id = pod["id"]
    print(f"created pod {pod_id} ({plan.pod_spec.name})")
    try:
        host, port = wait_for_pod_ready(client, pod_id, timeout_s=plan.boot_timeout_s)
        print(f"pod {pod_id} ready: ssh -p {port} root@{host}")

        upload_paths(host, port, plan.ssh_key, plan.uploads, plan.remote_workdir)
        print(f"uploaded {len(plan.uploads)} path(s) to {plan.remote_workdir}")

        if plan.remote_setup_command:
            run_remote_command(host, port, plan.ssh_key, plan.remote_setup_command)
            print("remote setup command finished")

        run_remote_command(host, port, plan.ssh_key, plan.remote_train_command)
        print("training command finished")

        download_path(host, port, plan.ssh_key, plan.artifact_remote_path, plan.artifact_local_path)
        print(f"artifact saved to {plan.artifact_local_path}")
        return 0
    finally:
        print(f"deleting pod {pod_id}")
        client.delete_pod(pod_id)


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    plan = build_plan(args)

    if args.dry_run:
        print(json.dumps(plan.to_dict(), indent=2))
        return 0

    api_key = args.api_key or os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        print("error: RUNPOD_API_KEY is not set (env var or --api-key)", file=sys.stderr)
        return 1

    return run_training(plan, api_key=api_key, api_base_url=args.api_base_url)


if __name__ == "__main__":
    sys.exit(main())
