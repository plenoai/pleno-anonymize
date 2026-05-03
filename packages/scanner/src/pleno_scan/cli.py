"""pleno-scan CLI."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

from pleno_scan import __version__
from pleno_scan.git_history import scan_history as _scan_history
from pleno_scan.github import list_org_repos, shallow_clone
from pleno_scan.ignore import IgnoreSet, filter_findings, load_baseline, write_baseline
from pleno_scan.models import Finding, ScanStats
from pleno_scan.regex_pass import compile_patterns, scan_files
from pleno_scan.report import render_human, render_json, render_sarif
from pleno_scan.verify import verify
from pleno_scan.walker import walk


def _common_options(f):
    f = click.option("--entities", default=None, help="Comma-separated entity types to scan for.")(f)
    f = click.option("--report-format", type=click.Choice(["human", "json", "sarif"]), default="human")(f)
    f = click.option("--report-path", type=click.Path(dir_okay=False, path_type=Path), default=None)(f)
    f = click.option("--baseline", "baseline_path", type=click.Path(dir_okay=False, path_type=Path), default=None)(f)
    f = click.option("--ignore-file", type=click.Path(dir_okay=False, path_type=Path), default=None)(f)
    f = click.option("--max-file-size", type=int, default=1024 * 1024, show_default=True)(f)
    f = click.option("--include", multiple=True, help="Glob to include (gitignore syntax).")(f)
    f = click.option("--exclude", multiple=True, help="Glob to exclude (gitignore syntax).")(f)
    f = click.option("--workers", type=int, default=None, help="Parallel workers (default: CPU count).")(f)
    f = click.option("--only-verified", is_flag=True, help="Suppress unverified/failed findings.")(f)
    f = click.option("--no-color", is_flag=True, help="Disable ANSI colors.")(f)
    f = click.option("--exit-zero", is_flag=True, help="Always exit 0 even when findings exist.")(f)
    return f


# Default profile: excludes high-noise patterns. Use --entities ALL to enable everything.
_NOISY_ENTITIES = frozenset({"URL", "HEALTH_INSURANCE", "DRIVER_LICENSE"})


def _select_recognizers(entities_csv: str | None):
    if not entities_csv:
        return tuple(r for r in ALL_JA_RECOGNIZERS if r.entity not in _NOISY_ENTITIES)
    if entities_csv.strip().upper() == "ALL":
        return ALL_JA_RECOGNIZERS
    wanted = {e.strip() for e in entities_csv.split(",") if e.strip()}
    selected = tuple(r for r in ALL_JA_RECOGNIZERS if r.entity in wanted)
    if not selected:
        raise click.UsageError(
            f"No recognizers match {sorted(wanted)}. "
            f"Known entities: {sorted({r.entity for r in ALL_JA_RECOGNIZERS})}"
        )
    return selected


def _resolve_ignore(ignore_file: Path | None, root: Path) -> IgnoreSet:
    candidate = ignore_file or (root / ".plenoignore")
    return IgnoreSet.load(candidate)


def _emit(stats: ScanStats, fmt: str, path: Path | None, color: bool) -> None:
    out = path.open("w", encoding="utf-8") if path else sys.stdout
    try:
        if fmt == "human":
            render_human(stats, out, color=color)
        elif fmt == "json":
            render_json(stats, out)
        elif fmt == "sarif":
            render_sarif(stats, out)
    finally:
        if path:
            out.close()


def _scan_directory(
    root: Path,
    *,
    entities: str | None,
    max_file_size: int,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    workers: int | None,
    ignore_set: IgnoreSet,
    baseline: set[str],
) -> ScanStats:
    recognizers = _select_recognizers(entities)
    patterns = compile_patterns(recognizers)

    t0 = time.monotonic()
    files = list(walk(
        root,
        max_file_size=max_file_size,
        include=list(include) if include else None,
        exclude=list(exclude) if exclude else None,
    ))

    file_pairs: list[tuple[Path, Path]] = []
    file_text: dict[str, str] = {}
    file_lines: dict[str, list[str]] = {}
    bytes_total = 0
    for full in files:
        try:
            rel = full.relative_to(root)
        except ValueError:
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel_str = rel.as_posix()
        file_pairs.append((rel, full))
        file_text[rel_str] = text
        file_lines[rel_str] = text.splitlines()
        bytes_total += len(text.encode("utf-8", errors="ignore"))

    findings = scan_files(file_pairs, patterns, workers=workers)
    findings = verify(findings, recognizers, file_text_for=file_text)
    kept, _ = filter_findings(
        findings, ignore_set=ignore_set, baseline=baseline, file_lines=file_lines
    )

    duration_ms = int((time.monotonic() - t0) * 1000)
    return ScanStats(
        files_scanned=len(file_pairs),
        bytes_scanned=bytes_total,
        duration_ms=duration_ms,
        findings=sorted(kept, key=lambda f: (f.file, f.line, f.col)),
    )


def _maybe_filter_verified(stats: ScanStats, only_verified: bool) -> ScanStats:
    if not only_verified:
        return stats
    stats.findings = [f for f in stats.findings if f.verification == "passed"]
    return stats


def _exit_code(stats: ScanStats, exit_zero: bool) -> int:
    if exit_zero or not stats.findings:
        return 0
    return 1


@click.group()
@click.version_option(__version__, prog_name="pleno-scan")
def main() -> None:
    """Scan source repositories for PII (Japanese-first)."""


@main.command(name="dir")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@_common_options
def cmd_dir(path: Path, entities, report_format, report_path, baseline_path,
            ignore_file, max_file_size, include, exclude, workers, only_verified,
            no_color, exit_zero) -> None:
    """Scan a directory tree."""
    ignore_set = _resolve_ignore(ignore_file, path)
    baseline = load_baseline(baseline_path) if baseline_path else set()
    stats = _scan_directory(
        path.resolve(),
        entities=entities,
        max_file_size=max_file_size,
        include=include,
        exclude=exclude,
        workers=workers,
        ignore_set=ignore_set,
        baseline=baseline,
    )
    stats = _maybe_filter_verified(stats, only_verified)
    color = sys.stdout.isatty() and not no_color and report_format == "human"
    _emit(stats, report_format, report_path, color)
    sys.exit(_exit_code(stats, exit_zero))


@main.command(name="git")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--no-history", is_flag=True, help="Skip git history pass.")
@click.option("--max-commits", type=int, default=None, help="Cap commits scanned.")
@_common_options
def cmd_git(path: Path, no_history, max_commits, entities, report_format, report_path,
            baseline_path, ignore_file, max_file_size, include, exclude, workers,
            only_verified, no_color, exit_zero) -> None:
    """Scan a local git repository (working tree + history)."""
    ignore_set = _resolve_ignore(ignore_file, path)
    baseline = load_baseline(baseline_path) if baseline_path else set()
    recognizers = _select_recognizers(entities)
    patterns = compile_patterns(recognizers)

    stats = _scan_directory(
        path.resolve(),
        entities=entities,
        max_file_size=max_file_size,
        include=include,
        exclude=exclude,
        workers=workers,
        ignore_set=ignore_set,
        baseline=baseline,
    )

    if not no_history:
        t0 = time.monotonic()
        hist_findings, n_commits = _scan_history(path.resolve(), patterns, max_commits=max_commits)
        hist_findings = verify(hist_findings, recognizers)
        kept, _ = filter_findings(hist_findings, ignore_set=ignore_set, baseline=baseline)
        stats.findings.extend(kept)
        stats.findings.sort(key=lambda f: (f.commit or "", f.file, f.line))
        stats.commits_scanned = n_commits
        stats.duration_ms += int((time.monotonic() - t0) * 1000)

    stats = _maybe_filter_verified(stats, only_verified)
    color = sys.stdout.isatty() and not no_color and report_format == "human"
    _emit(stats, report_format, report_path, color)
    sys.exit(_exit_code(stats, exit_zero))


@main.command(name="github")
@click.argument("target")
@click.option("--org", is_flag=True, help="Treat TARGET as a GitHub org and scan all repos.")
@click.option("--full", is_flag=True, help="Full clone (default: shallow depth=1).")
@click.option("--scan-history/--no-scan-history", "include_history", default=False, help="Scan git history (requires --full).")
@_common_options
def cmd_github(target, org, full, include_history, entities, report_format, report_path,
               baseline_path, ignore_file, max_file_size, include, exclude, workers,
               only_verified, no_color, exit_zero) -> None:
    """Clone a GitHub repo (or all repos in an org) and scan."""
    if include_history and not full:
        raise click.UsageError("--scan-history requires --full")

    targets = list_org_repos(target) if org else [target]

    aggregate = ScanStats()
    recognizers = _select_recognizers(entities)
    patterns = compile_patterns(recognizers)

    for slug in targets:
        click.echo(f"==> {slug}", err=True)
        with shallow_clone(slug, full=full) as repo:
            ignore_set = _resolve_ignore(ignore_file, repo)
            baseline = load_baseline(baseline_path) if baseline_path else set()
            sub = _scan_directory(
                repo,
                entities=entities,
                max_file_size=max_file_size,
                include=include,
                exclude=exclude,
                workers=workers,
                ignore_set=ignore_set,
                baseline=baseline,
            )
            # Re-prefix file paths so output is unambiguous across many repos.
            sub.findings = [
                Finding(**{**f.__dict__, "file": f"{slug}:{f.file}"}) for f in sub.findings
            ]
            aggregate.files_scanned += sub.files_scanned
            aggregate.bytes_scanned += sub.bytes_scanned
            aggregate.duration_ms += sub.duration_ms
            aggregate.findings.extend(sub.findings)

            if include_history:
                hist, n_commits = _scan_history(repo, patterns)
                hist = verify(hist, recognizers)
                hist, _ = filter_findings(hist, ignore_set=ignore_set, baseline=baseline)
                aggregate.findings.extend(
                    Finding(**{**h.__dict__, "file": f"{slug}:{h.file}"}) for h in hist
                )
                aggregate.commits_scanned += n_commits

    aggregate = _maybe_filter_verified(aggregate, only_verified)
    color = sys.stdout.isatty() and not no_color and report_format == "human"
    _emit(aggregate, report_format, report_path, color)
    sys.exit(_exit_code(aggregate, exit_zero))


@main.command(name="baseline")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--out", "out_path", type=click.Path(dir_okay=False, path_type=Path),
              default=Path(".plenoignore-baseline.json"), show_default=True)
@click.option("--entities", default=None)
@click.option("--max-file-size", type=int, default=1024 * 1024)
@click.option("--workers", type=int, default=None)
def cmd_baseline(path: Path, out_path: Path, entities, max_file_size, workers) -> None:
    """Capture current findings as a baseline file (suppresses them on later runs)."""
    stats = _scan_directory(
        path.resolve(),
        entities=entities,
        max_file_size=max_file_size,
        include=(),
        exclude=(),
        workers=workers,
        ignore_set=IgnoreSet(),
        baseline=set(),
    )
    write_baseline(out_path, stats.findings)
    click.echo(f"Wrote {len(stats.findings)} fingerprints → {out_path}", err=True)


@main.command(name="protect")
@click.option("--entities", default=None)
@click.option("--only-verified", is_flag=True)
@click.option("--no-color", is_flag=True)
def cmd_protect(entities, only_verified, no_color) -> None:
    """Pre-commit mode: scan staged hunks only. Exits non-zero on findings."""
    import subprocess

    diff = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--no-color"],
        capture_output=True, text=True, errors="replace",
    )
    if diff.returncode != 0:
        click.echo(diff.stderr, err=True)
        sys.exit(2)

    recognizers = _select_recognizers(entities)
    patterns = compile_patterns(recognizers)

    findings: list[Finding] = []
    current_file: str | None = None
    new_line = 0
    import re as _re
    file_re = _re.compile(r"^\+\+\+ b/(.+)$")
    hunk_re = _re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in diff.stdout.splitlines():
        if line.startswith("+++ "):
            m = file_re.match(line)
            current_file = m.group(1) if m else None
            continue
        if line.startswith("@@"):
            m = hunk_re.match(line)
            new_line = int(m.group(1)) if m else 0
            continue
        if current_file and line.startswith("+") and not line.startswith("+++"):
            text = line[1:]
            from pleno_scan.regex_pass import scan_text as _scan_text
            for f in _scan_text(text, current_file, patterns):
                findings.append(
                    Finding(**{**f.__dict__, "line": new_line})
                )
            new_line += 1

    findings = verify(findings, recognizers)
    if only_verified:
        findings = [f for f in findings if f.verification == "passed"]

    color = sys.stdout.isatty() and not no_color
    if not findings:
        click.echo("pleno-scan: no PII in staged hunks ✓", err=True)
        sys.exit(0)

    stats = ScanStats(findings=findings)
    render_human(stats, sys.stderr, color=color)
    click.echo(
        "pleno-scan: refusing commit. Use `# pleno:ignore <ENTITY>` if intentional.",
        err=True,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
