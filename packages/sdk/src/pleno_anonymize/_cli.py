"""``pleno-anonymize`` CLI.

Subcommands:
    scan      Walk paths and report PII per file (CI-friendly).
    analyze   Detect PII in text / stdin / --file.
    redact    Replace detected PII with ``<PLACEHOLDERS>``.
    models    Manage local NER model wheels (``install`` / ``status``).
    health    Ping a remote endpoint (only meaningful with --base-url).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence

from . import __version__
from ._engine import Engine, Finding, PlenoAnonymize
from ._models import LANGUAGE_TO_MODEL, MODEL_WHEELS, install, is_installed
from ._remote import PlenoAnonymizeError
from ._scanner import FileScanResult, ScanSummary, scan_paths

PROG = "pleno-anonymize"


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.no_color:
        _color.disable()

    try:
        handler = args.func
    except AttributeError:
        parser.print_help()
        return 0
    return handler(args)


# ----- parser ---------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Local-first PII detection / redaction. Use --base-url to target a hosted server instead.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI colors",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    _add_engine_args(
        sub.add_parser("scan", help="walk paths, detect PII per file"), include_io=False
    )
    _scan = sub.choices["scan"]
    _scan.add_argument(
        "paths", nargs="*", default=["."], help="files / directories (default: .)"
    )
    _scan.add_argument(
        "--max-bytes", type=int, default=256 * 1024, help="per-file byte cap"
    )
    _scan.add_argument("--workers", type=int, default=4, help="parallel scan workers")
    _scan.add_argument(
        "--ignore", default="", help="extra directory names to skip (comma-separated)"
    )
    _scan.add_argument(
        "--ext",
        default="",
        help="restrict to extensions (comma-separated, e.g. .md,.py)",
    )
    _scan.add_argument(
        "--fail-on-findings", action="store_true", help="exit 2 if any PII is detected"
    )
    _scan.set_defaults(func=_cmd_scan)

    _analyze = sub.add_parser("analyze", help="detect PII in text / stdin / --file")
    _add_engine_args(_analyze, include_io=True)
    _analyze.add_argument(
        "text", nargs="*", help="inline text (joined with spaces); falls back to stdin"
    )
    _analyze.set_defaults(func=_cmd_analyze)

    _redact = sub.add_parser("redact", help="replace detected PII with placeholders")
    _add_engine_args(_redact, include_io=True)
    _redact.add_argument("text", nargs="*", help="inline text; falls back to stdin")
    _redact.set_defaults(func=_cmd_redact)

    _models = sub.add_parser("models", help="manage local NER model wheels")
    msub = _models.add_subparsers(dest="models_command", metavar="MODELS_COMMAND")
    _mi = msub.add_parser("install", help="download a NER model wheel")
    _mi.add_argument("language", choices=sorted(LANGUAGE_TO_MODEL.keys()))
    _mi.add_argument("--quiet", action="store_true")
    _mi.set_defaults(func=_cmd_models_install)
    _ms = msub.add_parser("status", help="show installation status of all known models")
    _ms.set_defaults(func=_cmd_models_status)

    _h = sub.add_parser("health", help="ping --base-url and exit")
    _add_engine_args(_h, include_io=False)
    _h.set_defaults(func=_cmd_health)

    return parser


def _add_engine_args(p: argparse.ArgumentParser, *, include_io: bool) -> None:
    p.add_argument(
        "--base-url",
        default=None,
        help="hosted pleno-anonymize endpoint (env: PLENO_ANONYMIZE_BASE_URL); omit to run locally",
    )
    p.add_argument(
        "--api-key",
        default=None,
        help="bearer token for --base-url (env: PLENO_ANONYMIZE_API_KEY)",
    )
    p.add_argument(
        "--engine",
        default="builtin",
        choices=("builtin", "openai-privacy-filter"),
        help=(
            "detection backend (default: builtin = Presidio + spaCy NER). "
            "openai-privacy-filter uses the open-source OPF model "
            "(requires: pip install 'opf @ git+https://github.com/openai/privacy-filter@main')"
        ),
    )
    p.add_argument(
        "--opf-device",
        default=None,
        choices=("cpu", "cuda", "mps"),
        help="device for openai-privacy-filter (auto-detected by default)",
    )
    p.add_argument(
        "--opf-checkpoint",
        default=None,
        help="override OPF checkpoint dir (env: OPF_CHECKPOINT; default: ~/.opf/privacy_filter)",
    )
    p.add_argument("--language", default="ja", choices=("ja", "en"))
    p.add_argument(
        "--entities",
        default="",
        help="restrict to specific entity types (comma-separated)",
    )
    p.add_argument(
        "--no-auto-download",
        dest="auto_download",
        action="store_false",
        default=True,
        help="do not pip-install missing NER wheels (local mode only)",
    )
    p.add_argument("--json", action="store_true", help="emit JSON output")
    if include_io:
        p.add_argument("-f", "--file", default=None, help="read input text from file")


# ----- handlers -------------------------------------------------------------


def _cmd_scan(args: argparse.Namespace) -> int:
    engine = _make_engine(args)
    entities = _split(args.entities)
    extra_ignore = _split(args.ignore)
    include = _split(args.ext)

    is_json = bool(args.json)

    def _emit(file: FileScanResult) -> None:
        if is_json:
            return
        if file.skipped == "binary":
            return
        if file.error:
            sys.stderr.write(_color.yellow(f"! {file.path}: {file.error}\n"))
            return
        if not file.findings:
            return
        sys.stdout.write(_color.bold(f"{file.path}\n"))
        for f in file.findings:
            sys.stdout.write(
                f"  {_color.cyan(f.entity_type.ljust(18))} "
                f"{_color.dim(f'@{f.start}-{f.end} score={f.score:.2f}')}  "
                f"{_snippet(f)}\n"
            )

    summary = scan_paths(
        engine,
        args.paths,
        language=args.language,
        entities=entities,
        max_bytes=args.max_bytes,
        workers=args.workers,
        ignore=extra_ignore or None,
        include_extensions=include or None,
        on_file=_emit,
    )

    if is_json:
        json.dump(summary.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_summary(summary)

    if args.fail_on_findings and summary.total_findings > 0:
        return 2
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    text = _resolve_text(args)
    engine = _make_engine(args)
    findings = engine.analyze(
        text, language=args.language, entities=_split(args.entities)
    )
    if args.json:
        json.dump(
            [f.to_dict() for f in findings], sys.stdout, ensure_ascii=False, indent=2
        )
        sys.stdout.write("\n")
        return 0
    if not findings:
        sys.stdout.write(_color.green("no PII detected\n"))
        return 0
    for f in findings:
        sys.stdout.write(
            f"{_color.cyan(f.entity_type.ljust(18))} "
            f"{_color.dim(f'@{f.start}-{f.end} score={f.score:.2f}')}  {_snippet(f)}\n"
        )
    return 0


def _cmd_redact(args: argparse.Namespace) -> int:
    text = _resolve_text(args)
    engine = _make_engine(args)
    result = engine.redact(text, language=args.language, entities=_split(args.entities))
    if args.json:
        json.dump(result.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(f"{result.text}\n")
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    if not (args.base_url or os.environ.get("PLENO_ANONYMIZE_BASE_URL")):
        sys.stderr.write(
            _color.red("health requires --base-url (no remote configured)\n")
        )
        return 1
    engine = _make_engine(args)
    from ._remote import RemoteEngine

    if not isinstance(engine, RemoteEngine):
        sys.stderr.write(
            _color.red("health is only supported against a remote engine\n")
        )
        return 1
    try:
        result = engine.health()
    except PlenoAnonymizeError as e:
        sys.stderr.write(_color.red(f"error: {e}\n"))
        return 1
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(f"{result.get('status', 'ok')}\n")
    return 0


def _cmd_models_install(args: argparse.Namespace) -> int:
    name = LANGUAGE_TO_MODEL[args.language]
    if is_installed(name):
        sys.stdout.write(_color.green(f"{name} already installed\n"))
        return 0
    install(name, quiet=args.quiet)
    sys.stdout.write(_color.green(f"{name} installed\n"))
    return 0


def _cmd_models_status(_args: argparse.Namespace) -> int:
    for lang, name in sorted(LANGUAGE_TO_MODEL.items()):
        status = "installed" if is_installed(name) else "not installed"
        url = MODEL_WHEELS[name]
        sys.stdout.write(f"{lang}\t{name}\t{status}\t{url}\n")
    return 0


# ----- helpers --------------------------------------------------------------


def _make_engine(args: argparse.Namespace) -> Engine:
    base_url = getattr(args, "base_url", None)
    api_key = getattr(args, "api_key", None)
    auto_download = getattr(args, "auto_download", True)
    return PlenoAnonymize(
        base_url=base_url,
        api_key=api_key,
        languages=(args.language,),
        auto_download=auto_download,
        engine=getattr(args, "engine", "builtin"),
        opf_checkpoint=getattr(args, "opf_checkpoint", None),
        opf_device=getattr(args, "opf_device", None),
    )


def _resolve_text(args: argparse.Namespace) -> str:
    file_path = getattr(args, "file", None)
    if file_path:
        with open(file_path, encoding="utf-8") as fh:
            return fh.read()
    if args.text:
        return " ".join(args.text)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("provide text as an argument, --file, or via stdin")


def _split(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or None


def _snippet(f: Finding) -> str:
    flat = " ".join(f.text.split())
    return flat if len(flat) <= 60 else f"{flat[:57]}..."


def _print_summary(summary: ScanSummary) -> None:
    sys.stdout.write("\n")
    sys.stdout.write(
        _color.bold(
            f"scanned {summary.scanned_files} file(s), "
            f"{summary.total_findings} finding(s)\n"
        )
    )
    if summary.skipped_files:
        sys.stdout.write(_color.dim(f"skipped {summary.skipped_files} file(s)\n"))
    for entity, count in sorted(
        summary.by_entity.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        sys.stdout.write(f"  {_color.cyan(entity.ljust(18))} {count}\n")


# ----- color helper ---------------------------------------------------------


class _Color:
    def __init__(self) -> None:
        self.enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def disable(self) -> None:
        self.enabled = False

    def _wrap(self, code: str, s: str) -> str:
        return f"\x1b[{code}m{s}\x1b[0m" if self.enabled else s

    def red(self, s: str) -> str:
        return self._wrap("31", s)

    def yellow(self, s: str) -> str:
        return self._wrap("33", s)

    def green(self, s: str) -> str:
        return self._wrap("32", s)

    def cyan(self, s: str) -> str:
        return self._wrap("36", s)

    def dim(self, s: str) -> str:
        return self._wrap("2", s)

    def bold(self, s: str) -> str:
        return self._wrap("1", s)


_color = _Color()


if __name__ == "__main__":
    sys.exit(main())
