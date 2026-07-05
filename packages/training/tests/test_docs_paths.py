"""Verify `packages/training/` path references in agent-facing docs are real.

SKILL.md (`.claude/skills/ner-improve/SKILL.md`) and `MODEL_VERSIONING.md`
are read and trusted by autonomous agents. Issue #295: agents burned
iterations exploring paths those docs claimed existed but didn't, because
the docs drifted from the actual directory layout over time. This test
extracts every inline-code `packages/training/...` path reference from both
docs and asserts it resolves against the real repo tree, so drift is caught
by CI instead of by an agent mid-loop.

Dynamic path templates — glob segments like `v0.*` or placeholders like
`<name>` / `{language}` — cannot resolve to one literal path by design, so
only their static ancestor directory is required to exist.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_MD = REPO_ROOT / ".claude/skills/ner-improve/SKILL.md"
MODEL_VERSIONING_MD = REPO_ROOT / "packages/training/MODEL_VERSIONING.md"

# Fenced code blocks hold illustrative examples (e.g. a hypothetical
# experiment-log entry) that are not path references to verify.
_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_PATH_RE = re.compile(r"packages/training/[A-Za-z0-9_.\-/*<>{}]*")

# Any of these characters mark a path as a glob/placeholder template rather
# than a literal, resolvable path.
_DYNAMIC_MARKERS = re.compile(r"[*<>{}]")


def _extract_training_paths(doc: Path) -> set[str]:
    text = _FENCE_RE.sub("", doc.read_text(encoding="utf-8"))
    found: set[str] = set()
    for span in _INLINE_CODE_RE.findall(text):
        found.update(_PATH_RE.findall(span))
    return found


def _static_parent(path_str: str) -> str:
    """Directory that would *contain* instances of a glob/placeholder template.

    E.g. `data/processed/<name>/` -> `data` (`data/processed` itself is a
    gitignored, on-demand directory and legitimately absent until a training
    run creates it; `data` is not, so a deleted/renamed pipeline stage still
    fails this check).
    """
    static_part = _DYNAMIC_MARKERS.split(path_str, maxsplit=1)[0].rstrip("/")
    return static_part.rsplit("/", 1)[0]


def _assert_paths_resolve(paths: set[str]) -> None:
    missing = []
    for raw in sorted(paths):
        if _DYNAMIC_MARKERS.search(raw):
            parent = _static_parent(raw)
            if not (REPO_ROOT / parent).is_dir():
                missing.append(f"{raw!r} (static ancestor {parent!r} missing)")
            continue
        candidate = raw.rstrip("/")
        if not (REPO_ROOT / candidate).exists():
            missing.append(raw)
    assert not missing, (
        "doc references packages/training/ paths that do not exist:\n"
        + "\n".join(missing)
    )


def test_skill_md_training_paths_exist():
    paths = _extract_training_paths(SKILL_MD)
    assert paths, "expected packages/training/ path references in SKILL.md"
    _assert_paths_resolve(paths)


def test_model_versioning_md_training_paths_exist():
    paths = _extract_training_paths(MODEL_VERSIONING_MD)
    assert paths, "expected packages/training/ path references in MODEL_VERSIONING.md"
    _assert_paths_resolve(paths)
