"""Guard test for #294: pii-masking-300k is evaluation-only for this project.

`train_supervised_300k_en.py` trains on a local dump of the non-commercially
licensed `ai4privacy/pii-masking-300k` dataset. An autonomous agent (e.g.
`/ner-improve`) could mistake the filename for a usable training script and
run it, producing a derivative model published without the written
permission the license requires. The script must abort before any heavy
work (dataset load, model download, training) starts unless the caller
passes `--i-have-written-permission`.

This subprocess-invokes the real script with no flags, so it also pins that
the guard runs before the `datasets`/`transformers` imports — the test
completes in well under a second, no training or network I/O involved.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "train_supervised_300k_en.py"


def test_refuses_without_permission_flag() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1, (
        f"expected exit 1 without --i-have-written-permission, got {result.returncode}\n"
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "written permission" in result.stderr
    assert "AI4Privacy" in result.stderr


def test_help_mentions_permission_flag() -> None:
    """--help should surface the flag without tripping the guard (argparse exits 0 first)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--i-have-written-permission" in result.stdout
