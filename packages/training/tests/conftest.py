"""Shared pytest fixtures for pleno-ner-training tests.

Currently empty; per-test-file fixtures are defined locally. This file exists
so that pytest discovers `packages/training/tests/` as a test root (U1 scaffold).

`recognizers_ja.py` was relocated to `server/src/recognizers_ja.py` (#74) to
break the server→training workspace dep. Training-side tests still need to
import it for regex-correctness checks; expose it on sys.path so
`from recognizers_ja import ...` resolves to the server-canonical file.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SERVER_SRC = _REPO_ROOT / "server" / "src"
if str(_SERVER_SRC) not in sys.path:
    sys.path.insert(0, str(_SERVER_SRC))
