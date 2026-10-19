"""Guard against the setup-uv cache-key collision warning in CI.

`.github/workflows/ci.yml` runs five jobs (`check`, `test`, `sdk`, `extras`,
`training`) in parallel on identical `ubuntu-latest` runners, all restoring
and saving the uv cache from the same root `uv.lock`. `astral-sh/setup-uv`
derives its cache key from OS + arch + Python version + a hash of that
lockfile, so without a per-job `cache-suffix` every job computes the *same*
key and they race to reserve it. The losers annotate the run with:

    Failed to save: Unable to reserve cache with key setup-uv-...,
    another job may be creating this cache.

Assigning each job a distinct `cache-suffix` namespaces its cache key so
saves never contend. This test parses the workflow and fails if any
`setup-uv` step is missing a suffix, or if two steps share one — the exact
conditions that let the warning reappear.

Regex instead of a YAML parser on purpose: the test may run with only the
training dev group installed (pytest + jsonschema), which does not pull in
PyYAML, and a workflow regression test is not worth a new dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"

# A step is `- uses: ...` at six-space indent; its attributes and any
# trailing comments are the following lines indented at least eight spaces.
# Capture that body so we can look for `cache-suffix` on this step only.
_SETUP_UV_STEP_RE = re.compile(
    r"^ {6}- uses: astral-sh/setup-uv@[^\n]*\n(?P<body>(?: {8,}[^\n]*\n)*)",
    re.MULTILINE,
)
_CACHE_SUFFIX_RE = re.compile(r"^ {8,}cache-suffix:[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)


def _setup_uv_cache_suffixes() -> list[str | None]:
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    suffixes: list[str | None] = []
    for match in _SETUP_UV_STEP_RE.finditer(text):
        found = _CACHE_SUFFIX_RE.findall(match.group("body"))
        assert len(found) <= 1, (
            f"a single setup-uv step declared cache-suffix more than once: {found!r}"
        )
        suffixes.append(found[0] if found else None)
    return suffixes


def test_every_setup_uv_step_has_a_unique_cache_suffix():
    suffixes = _setup_uv_cache_suffixes()
    assert len(suffixes) >= 2, (
        "expected multiple setup-uv steps in ci.yml; parser found "
        f"{len(suffixes)} — update this test if the workflow was restructured"
    )

    missing = [i for i, suffix in enumerate(suffixes) if not suffix]
    assert not missing, (
        "ci.yml setup-uv steps without a cache-suffix would share the default "
        "cache key and race to save it (the 'Unable to reserve cache' warning): "
        f"step indices {missing}"
    )

    duplicates = sorted({s for s in suffixes if suffixes.count(s) > 1})
    assert not duplicates, (
        "ci.yml setup-uv steps share cache-suffix values, so their cache keys "
        f"collide and the save race warning can reappear: {duplicates!r}"
    )
