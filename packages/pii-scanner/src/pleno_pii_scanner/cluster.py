"""DB-clustering filter: surface only findings that look like a leaked DB.

A single email in a CODE_OF_CONDUCT.md is a maintainer contact, not a privacy
incident. The same email plus a name plus an address in the same row of a
CSV is a row of a personal database. Repository-level PII risk follows the
same rule: clustered findings are the high-risk signal, isolated mentions
are noise.

This module groups findings by file and folder and keeps only those whose
location forms a cluster meeting the configured thresholds. Designed to run
after ``verify`` and ``filter_noise`` in the scanner pipeline; the same
findings that survive structural filtering are then judged by their
co-occurrence pattern.

Defaults derived from the v0.2.3 ten-repo eval (see ``CHANGELOG.md``):
  - ``file_threshold = 2`` — two distinct findings in one file is the
    minimum bar for a record-shaped artifact (CSV row, fixture object).
  - ``folder_threshold = 3`` — a folder with three or more findings spread
    across files is the sharded-database pattern (per-user JSON files,
    per-record markdown).

Both thresholds are inclusive. A finding survives if **either** threshold
is met for its container.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from pleno_pii_scanner.models import Finding


@dataclass(frozen=True, slots=True)
class ClusterPolicy:
    """How aggressively to require clustering before reporting a finding.

    Distinct-value gating is what makes this a DB filter rather than a
    simple count. A file with the same support-contact email mentioned six
    times has six findings but only one identifiable individual — not a
    database. A file with three different author emails is. Counting raw
    findings without checking distinctness fails on this distinction.
    """

    file_threshold: int = 2
    file_min_distinct: int = 2
    folder_threshold: int = 3
    folder_min_distinct: int = 3
    # If True, a single high-impact entity in a file (e.g. MY_NUMBER passed)
    # is reported even without clustering. Off by default so the policy is
    # purely structural.
    keep_high_impact_singletons: bool = False


def _folder_of(file_path: str) -> str:
    return posixpath.dirname(file_path) or "."


def _is_high_impact(f: Finding) -> bool:
    """Singleton findings worth reporting regardless of clustering.

    Currently: only checksum-validated MY_NUMBER / MY_NUMBER_CORPORATE /
    CREDIT_CARD / BANK_ACCOUNT — these are individually severe enough that
    a single occurrence is still a legal/regulatory event.
    """
    if f.verification != "passed":
        return False
    return f.entity in {"MY_NUMBER", "MY_NUMBER_CORPORATE", "CREDIT_CARD", "BANK_ACCOUNT"}


def keep_db_clusters(
    findings: Iterable[Finding],
    *,
    policy: ClusterPolicy | None = None,
) -> list[Finding]:
    """Drop findings whose file/folder lacks both count AND distinctness.

    A finding survives if its container is **DB-shaped**:

      File-shaped DB: count >= ``file_threshold`` AND distinct matched
        values >= ``file_min_distinct``.

      Folder-shaped DB: count >= ``folder_threshold`` AND distinct matched
        values >= ``folder_min_distinct``.

    Or, if ``keep_high_impact_singletons`` is set, a checksum-validated
    high-impact entity (MY_NUMBER passed, CREDIT_CARD passed, …) is kept
    even without clustering.

    Returns a new list; input order is preserved.
    """
    policy = policy or ClusterPolicy()
    findings = list(findings)
    if not findings:
        return []

    # Cluster computation excludes findings whose checksum already failed —
    # those are known FPs (e.g. ISBN matched as MY_NUMBER, Tumblr post id
    # matched as MY_NUMBER) and must not promote a folder to "DB-shaped".
    counted = [f for f in findings if f.verification != "failed"]

    per_file: dict[str, list[Finding]] = defaultdict(list)
    per_folder: dict[str, list[Finding]] = defaultdict(list)
    for f in counted:
        per_file[f.file].append(f)
        per_folder[_folder_of(f.file)].append(f)

    file_qualifies: dict[str, bool] = {}
    for fp, fs in per_file.items():
        file_qualifies[fp] = (
            len(fs) >= policy.file_threshold
            and len({x.matched for x in fs}) >= policy.file_min_distinct
        )

    folder_qualifies: dict[str, bool] = {}
    for fld, fs in per_folder.items():
        folder_qualifies[fld] = (
            len(fs) >= policy.folder_threshold
            and len({x.matched for x in fs}) >= policy.folder_min_distinct
        )

    kept: list[Finding] = []
    for f in findings:
        if file_qualifies.get(f.file):
            kept.append(f)
            continue
        if folder_qualifies.get(_folder_of(f.file)):
            kept.append(f)
            continue
        if policy.keep_high_impact_singletons and _is_high_impact(f):
            kept.append(f)
            continue
    return kept


def cluster_summary(findings: Iterable[Finding]) -> dict[str, dict[str, int]]:
    """Return a per-file / per-folder count dictionary for diagnostics.

    Useful for explaining why a finding survived or was dropped — the CLI
    surfaces this when ``--db-only`` is on so users can see the cluster
    structure without reading the full finding list.
    """
    findings = list(findings)
    by_file: dict[str, int] = defaultdict(int)
    by_folder: dict[str, int] = defaultdict(int)
    by_file_entities: dict[str, set[str]] = defaultdict(set)
    for f in findings:
        by_file[f.file] += 1
        by_folder[_folder_of(f.file)] += 1
        by_file_entities[f.file].add(f.entity)
    return {
        "files": {f: c for f, c in by_file.items()},
        "folders": {f: c for f, c in by_folder.items()},
        "distinct_entities_per_file": {
            f: len(es) for f, es in by_file_entities.items()
        },
    }
