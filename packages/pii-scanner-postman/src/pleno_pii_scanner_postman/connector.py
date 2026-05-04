"""Postman SourceConnector — collection + environment scan with var resolution.

Pipeline:

  1. GET /workspaces → enumerate every workspace the API key
     can see (or apply explicit allowlist)
  2. Per workspace: GET /workspaces/{id} → list collections + envs
  3. Per collection: GET /collections/{id} → flatten the request
     tree (collections nest folders → folders nest requests)
  4. Per environment: GET /environments/{id} → key/value pairs
  5. For each request node, emit one Document containing:
       - method + url (with `{{vars}}` resolved against the
         relevant environment)
       - headers (key=value lines)
       - body (raw / form / file paths)
       - pre-request and test scripts (event.script.exec)
       - response examples (when `include_examples=True`)
  6. For each environment, optionally emit one Document with
     `key=value` lines (when `include_environments=True`)

The variable resolver applies environments **left-to-right** in
the order they appear in the workspace, with collection-scoped
variables winning over workspace-scoped ones — matches Postman's
own scope precedence (Local → Data → Environment → Collection →
Global).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec


_API_BASE = "https://api.getpostman.com"
_VAR_RE = re.compile(r"\{\{([^}]+)\}\}")


@dataclass(frozen=True, slots=True)
class PostmanConfig:
    """Construction config for `PostmanConnector`."""

    api_key: str
    workspaces: tuple[str, ...] = ()
    include_environments: bool = True
    include_examples: bool = True
    interlock_patterns: tuple[str, ...] = ()
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("api_key must be non-empty")

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # API key is sensitive; identify by hashed key + workspace set.
        import hashlib

        h = hashlib.sha256()
        h.update(self.api_key.encode())
        for w in sorted(self.workspaces):
            h.update(b"\0")
            h.update(w.encode())
        return f"postman:{h.hexdigest()[:16]}"


class PostmanConnector:
    """Read-only SourceConnector for Postman collections."""

    kind = "postman"

    def __init__(
        self,
        config: PostmanConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        if client is None:
            self._client = httpx.AsyncClient(
                base_url=_API_BASE, timeout=30.0
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        self._headers = {"X-Api-Key": config.api_key}
        # Pre-compiled patterns so we don't re-build per finding.
        self._interlock = [
            re.compile(p) for p in config.interlock_patterns
        ]
        # Cache for fetch(): map of ref.path → pre-rendered text.
        self._documents: dict[str, str] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=2,
            streaming=False,
        )

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        del cursor  # incremental=False
        workspaces = await self._list_workspaces()
        for ws in workspaces:
            ws_id = ws["id"]
            ws_name = ws.get("name", ws_id)
            detail = await self._get_json(f"/workspaces/{ws_id}")
            ws_detail = detail.get("workspace", {})
            env_values = await self._merge_environments(
                ws_detail.get("environments", []) or []
            )
            for coll_ref in ws_detail.get("collections", []) or []:
                coll_id = coll_ref["id"]
                coll = await self._get_json(f"/collections/{coll_id}")
                coll_obj = coll.get("collection", {})
                coll_name = coll_obj.get("info", {}).get("name", coll_id)
                # Collection-scoped variables override env vars.
                resolved_vars = dict(env_values)
                for var in coll_obj.get("variable", []) or []:
                    if var.get("key"):
                        resolved_vars[var["key"]] = str(var.get("value", ""))
                async for ref in self._walk_items(
                    items=coll_obj.get("item", []) or [],
                    path_prefix=f"{ws_name}/{coll_name}",
                    workspace_id=ws_id,
                    collection_id=coll_id,
                    variables=resolved_vars,
                    filter=filter,
                ):
                    yield ref
            if self._config.include_environments:
                for env_ref in ws_detail.get("environments", []) or []:
                    env_id = env_ref["id"]
                    env = await self._get_json(f"/environments/{env_id}")
                    env_obj = env.get("environment", {})
                    env_name = env_obj.get("name", env_id)
                    full = f"{ws_name}/__env__/{env_name}"
                    if filter.include and not _matches_any(
                        full, filter.include
                    ):
                        continue
                    if filter.exclude and _matches_any(full, filter.exclude):
                        continue
                    text = _serialise_environment(env_obj)
                    self._documents[full] = self._scrub(text)
                    yield DocumentRef(
                        source_id=self.id,
                        source_kind=self.kind,
                        path=full,
                        content_type="text/plain",
                        size=len(text),
                        metadata={
                            "workspace_id": ws_id,
                            "environment_id": env_id,
                            "kind": "environment",
                        },
                    )

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        text = self._documents.get(ref.path)
        if text is None:
            return
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
            extra=dict(ref.metadata),
        )

    async def close(self) -> None:
        self._documents.clear()
        if self._owns_client:
            await self._client.aclose()

    # --- internals ------------------------------------------------

    async def _list_workspaces(self) -> list[dict[str, Any]]:
        if self._config.workspaces:
            return [{"id": w, "name": w} for w in self._config.workspaces]
        body = await self._get_json("/workspaces")
        return body.get("workspaces", []) or []

    async def _merge_environments(
        self, env_refs: list[dict[str, Any]]
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        for env_ref in env_refs:
            env_id = env_ref["id"]
            env = await self._get_json(f"/environments/{env_id}")
            for v in env.get("environment", {}).get("values", []) or []:
                if not isinstance(v, Mapping):
                    continue
                if not v.get("enabled", True):
                    continue
                key = v.get("key")
                if key:
                    out[key] = str(v.get("value", ""))
        return out

    async def _walk_items(
        self,
        *,
        items: list[dict[str, Any]],
        path_prefix: str,
        workspace_id: str,
        collection_id: str,
        variables: Mapping[str, str],
        filter: SourceFilter,
        depth: int = 0,
    ) -> AsyncIterator[DocumentRef]:
        if depth > 100:
            # Defense against pathological collection nesting (cycles
            # are not legal in Postman v2.1 but we don't trust the wire).
            return
        for item in items:
            name = item.get("name", "")
            sub_items = item.get("item")
            if isinstance(sub_items, list):
                async for ref in self._walk_items(
                    items=sub_items,
                    path_prefix=f"{path_prefix}/{name}",
                    workspace_id=workspace_id,
                    collection_id=collection_id,
                    variables=variables,
                    filter=filter,
                    depth=depth + 1,
                ):
                    yield ref
                continue
            request = item.get("request")
            if not request:
                continue
            full = f"{path_prefix}/{name}"
            if filter.include and not _matches_any(full, filter.include):
                continue
            if filter.exclude and _matches_any(full, filter.exclude):
                continue
            text = _serialise_request(
                name=name,
                request=request,
                events=item.get("event", []) or [],
                responses=item.get("response", []) or []
                if self._config.include_examples
                else [],
                variables=variables,
            )
            self._documents[full] = self._scrub(text)
            yield DocumentRef(
                source_id=self.id,
                source_kind=self.kind,
                path=full,
                content_type="text/plain",
                size=len(text),
                metadata={
                    "workspace_id": workspace_id,
                    "collection_id": collection_id,
                    "request_name": name,
                    "kind": "request",
                },
            )

    def _scrub(self, text: str) -> str:
        # If interlock patterns are configured and one matches, redact
        # the matching segment in the returned Document text. The
        # FindingsStore (#9) still records the raw value envelope-
        # encrypted; this prevents the scanner's *own* output from
        # becoming the leak channel during rotation incidents.
        if not self._interlock:
            return text
        for pat in self._interlock:
            text = pat.sub("[REDACTED-INTERLOCK]", text)
        return text

    async def _get_json(self, path: str) -> dict[str, Any]:
        resp = await self._client.get(path, headers=self._headers)
        resp.raise_for_status()
        return resp.json()


# --- helpers ------------------------------------------------------


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(s, p) for p in patterns)


def _resolve_vars(value: str, variables: Mapping[str, str]) -> str:
    def sub(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        return variables.get(key, match.group(0))

    return _VAR_RE.sub(sub, value)


def _serialise_request(
    *,
    name: str,
    request: Mapping[str, Any] | str,
    events: list[Mapping[str, Any]],
    responses: list[Mapping[str, Any]],
    variables: Mapping[str, str],
) -> str:
    parts: list[str] = [f"name={name}"]
    if isinstance(request, str):
        # Shorthand form — request is just the URL string.
        parts.append(f"url={_resolve_vars(request, variables)}")
        return "\n".join(parts)
    method = request.get("method", "GET")
    url = _serialise_url(request.get("url"), variables)
    parts.append(f"method={method}")
    parts.append(f"url={url}")
    for h in request.get("header", []) or []:
        if not h.get("disabled"):
            parts.append(
                f"header.{h.get('key', '')}="
                f"{_resolve_vars(str(h.get('value', '')), variables)}"
            )
    auth = request.get("auth")
    if isinstance(auth, Mapping):
        atype = auth.get("type", "")
        for v in auth.get(atype, []) or []:
            if isinstance(v, Mapping) and v.get("key"):
                parts.append(
                    f"auth.{v['key']}="
                    f"{_resolve_vars(str(v.get('value', '')), variables)}"
                )
    body = request.get("body")
    if isinstance(body, Mapping):
        mode = body.get("mode", "")
        if mode == "raw":
            parts.append(
                f"body.raw={_resolve_vars(str(body.get('raw', '')), variables)}"
            )
        elif mode in {"urlencoded", "formdata"}:
            for kv in body.get(mode, []) or []:
                if isinstance(kv, Mapping) and not kv.get("disabled"):
                    parts.append(
                        f"body.{mode}.{kv.get('key', '')}="
                        f"{_resolve_vars(str(kv.get('value', '')), variables)}"
                    )
        elif mode == "file":
            file_obj = body.get("file") or {}
            src = file_obj.get("src", "") if isinstance(file_obj, Mapping) else ""
            parts.append(f"body.file={src}")
    for ev in events:
        if not isinstance(ev, Mapping):
            continue
        listen = ev.get("listen", "")
        script = ev.get("script") or {}
        exec_lines = script.get("exec", []) if isinstance(script, Mapping) else []
        if isinstance(exec_lines, str):
            exec_lines = [exec_lines]
        for line in exec_lines:
            parts.append(
                f"script.{listen}={_resolve_vars(str(line), variables)}"
            )
    for resp in responses:
        if not isinstance(resp, Mapping):
            continue
        body_raw = resp.get("body", "")
        if isinstance(body_raw, str) and body_raw:
            parts.append(
                f"example.{resp.get('name', 'unnamed')}="
                f"{_resolve_vars(body_raw, variables)}"
            )
    return "\n".join(parts)


def _serialise_url(
    url: Any, variables: Mapping[str, str]
) -> str:
    if isinstance(url, str):
        return _resolve_vars(url, variables)
    if not isinstance(url, Mapping):
        return ""
    raw = url.get("raw")
    if isinstance(raw, str):
        return _resolve_vars(raw, variables)
    # Compose from parts when raw isn't present.
    proto = url.get("protocol", "")
    host = url.get("host")
    if isinstance(host, list):
        host = ".".join(str(h) for h in host)
    elif not isinstance(host, str):
        host = ""
    path = url.get("path")
    if isinstance(path, list):
        path = "/".join(str(p) for p in path)
    elif not isinstance(path, str):
        path = ""
    return _resolve_vars(f"{proto}://{host}/{path}", variables)


def _serialise_environment(env: Mapping[str, Any]) -> str:
    parts: list[str] = [f"environment={env.get('name', '')}"]
    for v in env.get("values", []) or []:
        if not isinstance(v, Mapping) or not v.get("key"):
            continue
        if not v.get("enabled", True):
            continue
        parts.append(f"{v['key']}={v.get('value', '')}")
    return "\n".join(parts)


# --- factory / spec -----------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    if "api_key" not in config:
        raise ValueError("postman connector config requires 'api_key'")
    return PostmanConnector(
        PostmanConfig(
            api_key=str(config["api_key"]),
            workspaces=tuple(str(w) for w in config.get("workspaces", ())),
            include_environments=bool(
                config.get("include_environments", True)
            ),
            include_examples=bool(config.get("include_examples", True)),
            interlock_patterns=tuple(
                str(p) for p in config.get("interlock_patterns", ())
            ),
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="postman",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=False,
        binary=False,
        content_hash_delta=False,
        max_concurrent_fetches=2,
        streaming=False,
    ),
    required_scopes=("postman:read",),
    description=(
        "Postman SourceConnector. Walks every collection in every "
        "workspace the API key can see; resolves {{var}} placeholders "
        "against environment + collection scoped variables; serialises "
        "request URL/headers/body/scripts and optional response examples."
    ),
)


__all__ = ["PostmanConfig", "PostmanConnector", "SPEC"]
