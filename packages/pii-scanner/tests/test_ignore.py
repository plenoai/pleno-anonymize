from pathlib import Path

from pleno_pii_scanner.ignore import (
    IgnoreSet,
    _inline_ignored_entities,
    filter_findings,
    write_baseline,
    load_baseline,
)
from pleno_pii_scanner.models import Finding


def _f(entity="EMAIL_ADDRESS", file="a.py", line=1, matched="x@y.com"):
    return Finding(
        entity=entity,
        file=file,
        line=line,
        col=1,
        score=0.9,
        snippet="snip",
        matched=matched,
        pattern_name="p",
    )


def test_inline_ignore_specific():
    assert _inline_ignored_entities("x = 1  # pleno:ignore PHONE_NUMBER") == {
        "PHONE_NUMBER"
    }


def test_inline_ignore_all():
    assert _inline_ignored_entities("x = 1  # pleno:ignore") == set()


def test_inline_ignore_multiple():
    assert _inline_ignored_entities("# pleno:ignore PHONE_NUMBER,EMAIL_ADDRESS") == {
        "PHONE_NUMBER",
        "EMAIL_ADDRESS",
    }


def test_inline_ignore_none():
    assert _inline_ignored_entities("just code, no directive") is None


def test_ignoreset_loads(tmp_path: Path):
    p = tmp_path / ".plenoignore"
    p.write_text("# comment\ndocs/**\nPHONE_NUMBER\nfinding:abc123\n")
    s = IgnoreSet.load(p)
    assert "PHONE_NUMBER" in s.entities
    assert "abc123" in s.fingerprints
    assert s.path_spec.match_file("docs/sample.txt")


def test_filter_findings_entity():
    s = IgnoreSet(entities={"EMAIL_ADDRESS"})
    kept, suppressed = filter_findings([_f()], ignore_set=s, baseline=set())
    assert kept == []
    assert len(suppressed) == 1


def test_filter_findings_baseline():
    f = _f()
    kept, suppressed = filter_findings(
        [f], ignore_set=IgnoreSet(), baseline={f.fingerprint()}
    )
    assert kept == []


def test_filter_findings_inline():
    f = _f(file="a.py", line=2, matched="x@y.com")
    file_lines = {"a.py": ["line1", "x = 'x@y.com'  # pleno:ignore EMAIL_ADDRESS"]}
    kept, _ = filter_findings(
        [f], ignore_set=IgnoreSet(), baseline=set(), file_lines=file_lines
    )
    assert kept == []


def test_baseline_roundtrip(tmp_path: Path):
    p = tmp_path / "baseline.json"
    f = _f()
    write_baseline(p, [f])
    fps = load_baseline(p)
    assert f.fingerprint() in fps
