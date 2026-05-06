"""DB-cluster filter tests.

Cases reflect the design rule: a single isolated finding is not a database;
co-occurrence in a file or folder is. Anchored to real-world risk shapes
seen in the v0.2.3 ten-repo eval.
"""

from pleno_pii_scanner.cluster import ClusterPolicy, keep_db_clusters
from pleno_pii_scanner.models import Finding


def _f(entity, file, line=1, matched="x", verification="unverified", score=0.5):
    return Finding(
        entity=entity,
        file=file,
        line=line,
        col=1,
        score=score,
        snippet=matched,
        matched=matched,
        pattern_name="p",
        verification=verification,
    )


# ---------------------------------------------------------------------------
# File-level clustering: >=2 findings in one file is enough.
# ---------------------------------------------------------------------------


def test_drops_isolated_singleton_finding():
    fs = [_f("EMAIL_ADDRESS", "CODE_OF_CONDUCT.md", matched="azuciao@gmail.com")]
    assert keep_db_clusters(fs) == []


def test_keeps_pair_in_same_file():
    fs = [
        _f("EMAIL_ADDRESS", "tests/fixtures.py", line=1, matched="taro@x.jp"),
        _f("PHONE_NUMBER", "tests/fixtures.py", line=2, matched="090-0000-0000"),
    ]
    assert len(keep_db_clusters(fs)) == 2


def test_drops_same_email_repeated_in_one_file():
    """Six mentions of one support contact = one identifiable individual,
    not a DB. Distinct-value gating must drop this."""
    same = "support@example-corp.jp"
    fs = [_f("EMAIL_ADDRESS", "faq.md", line=i, matched=same) for i in range(1, 7)]
    assert keep_db_clusters(fs) == []


def test_drops_same_email_repeated_across_files_in_one_folder():
    """The same support email scattered across docs is still one
    individual, not a sharded DB."""
    same = "support@example-corp.jp"
    fs = [
        _f("EMAIL_ADDRESS", "docs/intro.md", line=1, matched=same),
        _f("EMAIL_ADDRESS", "docs/setup.md", line=1, matched=same),
        _f("EMAIL_ADDRESS", "docs/faq.md", line=1, matched=same),
        _f("EMAIL_ADDRESS", "docs/api.md", line=1, matched=same),
    ]
    assert keep_db_clusters(fs) == []


def test_keeps_csv_row_with_multiple_entities():
    # Realistic record-shape: name + phone + email + my_number on one row
    fs = [
        _f("PERSON", "data/customers.csv", line=2, matched="山田太郎"),
        _f("PHONE_NUMBER", "data/customers.csv", line=2, matched="090-1234-5678"),
        _f("EMAIL_ADDRESS", "data/customers.csv", line=2, matched="taro@a.jp"),
        _f("MY_NUMBER", "data/customers.csv", line=2, matched="123456789012"),
    ]
    assert len(keep_db_clusters(fs)) == 4


# ---------------------------------------------------------------------------
# Folder-level clustering: >=3 findings spread across files in same folder.
# ---------------------------------------------------------------------------


def test_keeps_findings_when_folder_meets_threshold():
    # Sharded DB shape: per-record markdown, single email per file, but 3+
    # files in the same folder.
    fs = [
        _f("EMAIL_ADDRESS", "members/yamada.md", matched="yamada@x.jp"),
        _f("EMAIL_ADDRESS", "members/tanaka.md", matched="tanaka@x.jp"),
        _f("EMAIL_ADDRESS", "members/sato.md", matched="sato@x.jp"),
    ]
    kept = keep_db_clusters(fs)
    assert len(kept) == 3


def test_drops_when_folder_below_threshold():
    # Two folders, one finding each → no cluster.
    fs = [
        _f("EMAIL_ADDRESS", "a/x.md", matched="a@x.jp"),
        _f("EMAIL_ADDRESS", "b/x.md", matched="b@x.jp"),
    ]
    assert keep_db_clusters(fs) == []


def test_drops_two_findings_in_separate_folders():
    fs = [
        _f("EMAIL_ADDRESS", "a/x.md", matched="a@x.jp"),
        _f("EMAIL_ADDRESS", "b/y.md", matched="b@x.jp"),
    ]
    # File threshold (2) not met for either, folder threshold (3) not met
    # because each folder has only 1 finding.
    assert keep_db_clusters(fs) == []


# ---------------------------------------------------------------------------
# Mixed scenarios.
# ---------------------------------------------------------------------------


def test_keeps_only_clustered_subset_when_mixed():
    fs = [
        # Cluster: tests/ folder has 4 distinct-value findings across 2 files.
        _f("EMAIL_ADDRESS", "tests/fixtures.py", line=1, matched="taro@x.jp"),
        _f("PHONE_NUMBER", "tests/fixtures.py", line=2, matched="090-1111-2222"),
        _f("PERSON", "tests/expected.json", line=1, matched="山田太郎"),
        _f("PERSON", "tests/expected.json", line=2, matched="佐藤花子"),
        # Isolated: README.md, single email — drop.
        _f("EMAIL_ADDRESS", "README.md", matched="contact@x.jp"),
    ]
    kept = keep_db_clusters(fs)
    assert len(kept) == 4
    assert all(f.file != "README.md" for f in kept)


# ---------------------------------------------------------------------------
# High-impact-singleton policy (off by default).
# ---------------------------------------------------------------------------


def test_high_impact_singleton_kept_when_policy_enabled():
    # Verified MY_NUMBER is severe enough on its own.
    fs = [
        _f("MY_NUMBER", "leak.txt", matched="123456789012", verification="passed"),
    ]
    policy = ClusterPolicy(keep_high_impact_singletons=True)
    assert len(keep_db_clusters(fs, policy=policy)) == 1


def test_high_impact_singleton_dropped_when_unverified():
    fs = [
        _f("MY_NUMBER", "leak.txt", matched="123456789012", verification="unverified"),
    ]
    policy = ClusterPolicy(keep_high_impact_singletons=True)
    assert keep_db_clusters(fs, policy=policy) == []


def test_failed_findings_do_not_count_toward_cluster():
    """ISBNs flagged as MY_NUMBER (checksum failed) must not promote a
    folder to DB-shaped. Otherwise an awesome-list of book links would
    look like a leaked DB."""
    fs = [
        _f(
            "MY_NUMBER_CORPORATE",
            "books.md",
            line=1,
            matched="9784911384039",
            verification="failed",
        ),
        _f(
            "MY_NUMBER_CORPORATE",
            "books.md",
            line=2,
            matched="9784000000000",
            verification="failed",
        ),
        _f(
            "MY_NUMBER_CORPORATE",
            "books.md",
            line=3,
            matched="9784912345678",
            verification="failed",
        ),
    ]
    # No real PII finding → cluster computation excludes the failed ones,
    # nothing qualifies, all dropped.
    assert keep_db_clusters(fs) == []


# ---------------------------------------------------------------------------
# Threshold tunability.
# ---------------------------------------------------------------------------


def test_custom_thresholds():
    fs = [
        _f("EMAIL_ADDRESS", "tests/a.py", line=1, matched="a@x.jp"),
        _f("PHONE_NUMBER", "tests/a.py", line=2, matched="090-1111-1111"),
        _f("PERSON", "tests/a.py", line=3, matched="山田太郎"),
    ]
    # Stricter file threshold of 4 → no cluster.
    policy = ClusterPolicy(file_threshold=4, folder_threshold=10)
    assert keep_db_clusters(fs, policy=policy) == []
