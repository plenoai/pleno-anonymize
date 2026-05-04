"""TOML-file credential resolver.

Reads `~/.config/pleno/credentials.toml` (XDG-aware) or an explicit path
passed to the constructor (for `--credentials-file PATH`). Plain TOML
only — SOPS / age decryption is intentionally out of scope here and
hooked in later via plugin (see ADR-0007 §3 follow-up).
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pleno_pii_scanner.credentials.broker import (
    Credential,
    CredentialMisconfiguredError,
)


def default_credentials_path() -> Path:
    """Return the platform credentials path honoring XDG_CONFIG_HOME.

    Resolved every call (not cached) so tests can manipulate the env
    without monkey-patching this module.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "pleno" / "credentials.toml"


class FileCredentialResolver:
    """TOML-backed CredentialResolver.

    Layout::

        [github.default]
        kind = "github-pat"
        token = "ghp_xxx"

        [aws.prod]
        kind = "aws-iam"
        access_key_id = "..."
        secret_access_key = "..."

    The top-level table key is the credential kind family (`github`,
    `aws`, ...) and the second key is the credential name. The mandatory
    `kind` field inside the inner table is the *specific* kind string
    (`github-pat`, `github-app`, `aws-iam`, `aws-oidc`) so one file can
    mix flavors of the same family.
    """

    name = "file"

    def __init__(self, path: Path | None = None, *, priority: int = 100) -> None:
        # Resolve path lazily on each load so a test that creates the
        # file after the resolver is constructed still sees it.
        self._explicit_path = path
        self.priority = priority
        self._cache: dict[Path, dict[str, Any]] = {}

    def _resolve_path(self) -> Path:
        return self._explicit_path or default_credentials_path()

    def _load(self) -> dict[str, Any]:
        path = self._resolve_path()
        # Cache per absolute path — re-reading TOML on every credential
        # lookup would be wasteful, but mtime invalidation would force
        # disk seeks. Process-lifetime cache is the correct trade-off
        # for a CLI / one-shot scan; long-running daemons should
        # construct a fresh resolver on rotation.
        if path in self._cache:
            return self._cache[path]
        if not path.exists():
            self._cache[path] = {}
            return self._cache[path]
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise CredentialMisconfiguredError(
                f"credentials file {path} is not valid TOML: {exc}"
            ) from exc
        # Surface SOPS-encrypted markers loudly so a user does not
        # silently get an "unknown" credential because the resolver
        # cannot decrypt. Marker convention matches sops outputs.
        if "sops" in data or data.get("encrypted") is True:
            raise NotImplementedError(
                "SOPS support not yet wired; supply plain TOML for now"
            )
        self._cache[path] = data
        return data

    async def resolve(self, kind: str, name: str) -> Credential | None:
        data = self._load()
        family, _, _ = kind.partition("-")
        # Two lookup keys are tried so the TOML can be organized either
        # by family ([github.default]) or by exact kind ([github-pat.default]).
        # Family form is the documented default; exact form is a power-user
        # escape hatch when one family hosts incompatible flavors that
        # should not share a name namespace.
        for top_key in (family, kind):
            section = data.get(top_key)
            if not isinstance(section, Mapping):
                continue
            entry = section.get(name)
            if not isinstance(entry, Mapping):
                continue
            payload, declared_kind = self._materialize_entry(entry, top_key, name)
            if declared_kind != kind:
                # Wrong flavor under this name — keep searching, the
                # exact-kind lookup may still match.
                continue
            return Credential(
                kind=kind,
                payload=payload,
                source=f"file:{self._resolve_path()}#{top_key}.{name}",
            )
        return None

    def _materialize_entry(
        self, entry: Mapping[str, Any], top_key: str, name: str
    ) -> tuple[dict[str, object], str]:
        # Copy because TOML returns a live dict we must not mutate, and
        # because we expand `*_path` fields into the file contents.
        payload: dict[str, object] = {}
        declared_kind: str | None = None
        for k, v in entry.items():
            if k == "kind":
                if not isinstance(v, str):
                    raise CredentialMisconfiguredError(
                        f"[{top_key}.{name}] kind must be a string, got {type(v).__name__}"
                    )
                declared_kind = v
                continue
            if isinstance(k, str) and k.endswith("_path") and isinstance(v, str):
                # Expand "private_key_path" -> "private_key" with file
                # contents so connectors do not each re-implement file
                # reading + tilde expansion.
                key_no_suffix = k[: -len("_path")]
                expanded = Path(v).expanduser()
                try:
                    payload[key_no_suffix] = expanded.read_text(encoding="utf-8")
                except OSError as exc:
                    raise CredentialMisconfiguredError(
                        f"[{top_key}.{name}] cannot read {k}={v!r}: {exc}"
                    ) from exc
                continue
            payload[k] = v
        if declared_kind is None:
            raise CredentialMisconfiguredError(
                f"[{top_key}.{name}] missing required `kind` field"
            )
        return payload, declared_kind
