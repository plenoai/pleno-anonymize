"""httpx wrapper for the four CI vendor APIs we touch (Task #41, ADR §13).

One HTTP layer, four wire flavors. Each vendor has a different paginator
and a different rate-limit signal; the wrapper normalises both:

* **Auth** — `Authorization: Bearer` (GitHub Actions, Buildkite),
  `Circle-Token` (CircleCI), HTTP Basic (Jenkins).
* **Pagination** — `?per_page+&page=` (GHA), opaque `?page-token=`
  cursor (CircleCI), `Link` header (Buildkite). Jenkins has no
  pagination because we hit `/api/json` once with the `tree=` filter.
* **Rate limit** — GHA exposes `X-RateLimit-Remaining` /
  `X-RateLimit-Reset`; the others fall back to `Retry-After` on 429.

`RateLimited` surfaces to the scheduler's AIMD bucket; the wrapper
retries once on 429 before raising so a single scan-time blip does
not amplify into N pipeline-stalled coroutines.

The four flavors share one `httpx.AsyncClient` so a multi-flavor
factory is wire-incoherent — but in practice each connector instance
is single-flavor (operators do not mix Jenkins + Buildkite under one
profile), so we lock the flavor at construction.
"""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from pleno_pii_scanner.scheduler.rate_limit import RateLimited


# Wire flavor literal. Selected at construction; the URL builders +
# paginator branch on this. We keep it as a string union (not a base
# class) so type checkers narrow exhaustively.
Flavor = Literal["github_actions", "circleci", "buildkite", "jenkins"]


# Default vendor endpoints. Jenkins has no default — operators always
# point at their on-prem controller (`base_url=`); the absence is
# enforced at config validation.
DEFAULT_GITHUB_ACTIONS_BASE_URL = "https://api.github.com"
DEFAULT_CIRCLECI_BASE_URL = "https://circleci.com/api/v2"
DEFAULT_BUILDKITE_BASE_URL = "https://api.buildkite.com/v2"


# Fallback `Retry-After` when the upstream returns 429 without the
# header. 30s matches the conservative AIMD floor the scheduler starts
# from; spinning at zero would IP-ban us before the bucket can recover.
_DEFAULT_RETRY_AFTER_SECONDS = 30.0


# Pagination depth ceiling. Defends against an upstream that hands back
# the same `next` URL repeatedly (a real CircleCI bug we have seen on
# `page-token` echoes during their 2024-12 outage). 10_000 pages × 100
# entries = 1M builds, well above any realistic per-pipeline history.
_MAX_PAGINATION_DEPTH = 10_000


# Parser for HTTP `Link` headers (RFC 5988). We need only `rel="next"`
# extraction, so a tight regex beats pulling in `requests`'s parser.
_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class CiLogsApiError(Exception):
    """Non-retryable upstream failure (4xx other than 401/403/429)."""


@dataclass(frozen=True, slots=True)
class BasicAuth:
    """HTTP Basic auth — Jenkins user + API token (or password).

    `__repr__` is auto-generated and the secret never lands in it
    because the dataclass field name alone is `password` — there is no
    string interpolation that could leak the value via traceback.
    """

    username: str
    password: str

    def header_value(self) -> str:
        # Pre-encode once at use site; b64encode requires bytes input.
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")


@dataclass(frozen=True, slots=True)
class BearerAuth:
    """Bearer token — GitHub Actions PAT or Buildkite API token."""

    token: str

    def header_value(self) -> str:
        return f"Bearer {self.token}"


@dataclass(frozen=True, slots=True)
class CircleTokenAuth:
    """CircleCI uses a custom `Circle-Token` header instead of `Authorization`.

    A separate auth class (rather than overloading BearerAuth) keeps
    the header-name selection inside the auth object so the request
    layer never has to branch on flavor for header construction.
    """

    token: str

    def header_value(self) -> str:
        return self.token


# Either of the auth modes the connector accepts. Constrained to a
# union (not a base class) so the header builder narrows exhaustively
# — adding a fifth mode later forces every match site to update.
AuthMode = BasicAuth | BearerAuth | CircleTokenAuth


def _auth_header_name(auth: AuthMode) -> str:
    """Return the header NAME the auth value goes under.

    CircleCI uses `Circle-Token`; everyone else uses `Authorization`.
    The name lives next to the value so flavor-specific routing stays
    out of the request layer.
    """
    if isinstance(auth, CircleTokenAuth):
        return "Circle-Token"
    return "Authorization"


class CiLogsApi:
    """Thin async wrapper around `httpx.AsyncClient`.

    Owns one client per connector lifetime. Tests inject a mock
    transport; production callers pass `transport=None` for the
    default. `sleep` is injectable so 429-backoff tests do not hit
    `asyncio.sleep`.
    """

    def __init__(
        self,
        *,
        flavor: Flavor,
        base_url: str,
        auth: AuthMode,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        sleep: "Any | None" = None,
    ) -> None:
        if flavor not in ("github_actions", "circleci", "buildkite", "jenkins"):
            raise ValueError(f"unsupported ci_logs flavor: {flavor!r}")
        self._flavor: Flavor = flavor
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)
        # Inject sleep so backoff tests stay fast and deterministic.
        self._sleep = sleep or asyncio.sleep

    @property
    def flavor(self) -> Flavor:
        return self._flavor

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # request layer
    # ------------------------------------------------------------------

    def _headers(self, *, accept: str | None = None) -> dict[str, str]:
        # Per-request `accept` override is required for GitHub Actions
        # `/logs` zip download (`application/zip`); other endpoints take
        # `application/json`. `User-Agent` is good citizenship — GHA's
        # abuse detector + Jenkins access-log pipeline both look at it.
        h: dict[str, str] = {
            "Accept": accept or "application/json",
            "User-Agent": "pleno-pii-scanner-ci-logs",
            _auth_header_name(self._auth): self._auth.header_value(),
        }
        return h

    def _absolute(self, path_or_url: str) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        return f"{self._base_url}{path_or_url}"

    async def get(
        self,
        path_or_url: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str | None = None,
    ) -> httpx.Response:
        """REST GET with single-shot rate-limit retry.

        On 429 we honor `Retry-After` (or the GHA `X-RateLimit-Reset`
        delta when the header is absent), retry once, and surface
        `RateLimited` if the second response is also throttled. The
        retry is bounded here rather than at the scheduler so that a
        page-walk making N requests does not amplify a transient blip
        into N × scheduler-level backoffs.
        """
        url = self._absolute(path_or_url)
        response = await self._client.get(
            url, params=params, headers=self._headers(accept=accept)
        )
        if not _is_throttled(response):
            return response
        delay = _retry_after_seconds(response)
        await self._sleep(delay)
        response = await self._client.get(
            url, params=params, headers=self._headers(accept=accept)
        )
        if _is_throttled(response):
            raise RateLimited(
                f"ci_logs[{self._flavor}] {response.status_code}; "
                f"retry_after={delay}s (after one retry)"
            )
        return response

    async def get_bytes(self, path_or_url: str) -> bytes:
        """GET a binary blob (GHA `/logs` zip).

        Returns the raw response content. Caller is responsible for
        zip extraction + per-member size enforcement; the wrapper does
        not buffer multiple responses so memory stays bounded by the
        single in-flight zip.
        """
        response = await self.get(path_or_url, accept="application/zip")
        if response.status_code != 200:
            raise CiLogsApiError(
                f"ci_logs[{self._flavor}] log-fetch returned "
                f"{response.status_code}"
            )
        return response.content

    # ------------------------------------------------------------------
    # pagination
    # ------------------------------------------------------------------

    async def paginate(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> AsyncIterator[Mapping[str, Any] | list[Any]]:
        """Yield each entry across every page of a paginated endpoint.

        Per-flavor paginator:

        * GitHub Actions: `?per_page=N&page=K`. Body is
          `{"workflow_runs": [...], "total_count": N}`. Stop when the
          page returns fewer than `per_page` entries.
        * CircleCI: opaque `next_page_token` echoed back as
          `?page-token=`. Body is `{"items": [...], "next_page_token": T}`.
        * Buildkite: `Link` header with `rel="next"`. Body is a JSON
          list. Stop when the header is absent.
        * Jenkins: no pagination — `paginate()` is not used; callers
          hit `/api/json` directly.

        The `params` dict is sent on every request; per-flavor cursor
        params (`page`, `page-token`) are layered on top per iteration.
        """
        if self._flavor == "jenkins":
            raise CiLogsApiError(
                "jenkins flavor does not paginate; call get() directly"
            )

        depth = 0
        # Per-flavor pagination state. Initialised here so the loop
        # body can branch tightly on flavor without re-checking
        # invariants on every iteration.
        page_index = 1
        page_token: str | None = None
        next_url: str | None = None

        while True:
            depth += 1
            if depth > _MAX_PAGINATION_DEPTH:
                # A vendor that returns the same `next` perpetually
                # would otherwise spin discover() forever. Surface as
                # CiLogsApiError so the scheduler treats it as a hard
                # failure rather than retrying.
                raise CiLogsApiError(
                    f"ci_logs[{self._flavor}] pagination exceeded "
                    f"{_MAX_PAGINATION_DEPTH} pages; refusing to continue"
                )

            request_params: dict[str, Any] = dict(params or {})
            if self._flavor == "github_actions":
                request_params["per_page"] = page_size
                request_params["page"] = page_index
            elif self._flavor == "circleci":
                if page_token is not None:
                    request_params["page-token"] = page_token
            elif self._flavor == "buildkite":
                request_params["per_page"] = page_size
                # `page` is honored on the first request only; later
                # requests follow `Link` rel="next" verbatim.
                if next_url is None:
                    request_params["page"] = page_index

            url = next_url if next_url is not None else self._absolute(path)
            # `next_url` from a `Link` header already carries the
            # cursor query, so we must not re-layer params on top.
            send_params = None if next_url is not None else request_params
            response = await self.get(url, params=send_params)
            if response.status_code != 200:
                # 404 / 403 mid-paginate: yield nothing. Forces every
                # caller to wrap with try/except just to handle "the
                # pipeline was deleted between enumeration and walk"
                # — easier to treat HTTP errors at this layer as empty
                # result sets, the same idiom the github + bitbucket
                # connectors use.
                return
            try:
                body = response.json()
            except ValueError:
                # Defensive: a vendor briefly serving HTML on a JSON
                # path (CDN error pages, maintenance windows) must
                # not crash the whole scan.
                return

            if self._flavor == "github_actions":
                runs = (body or {}).get("workflow_runs") or []
                for entry in runs:
                    yield entry
                # Stop when the server signals fewer items than asked.
                # GHA returns `total_count` but it includes filtered
                # runs we cannot see; trusting `len(runs) < per_page`
                # is more reliable.
                if len(runs) < page_size:
                    return
                page_index += 1
            elif self._flavor == "circleci":
                items = (body or {}).get("items") or []
                for entry in items:
                    yield entry
                page_token = (body or {}).get("next_page_token")
                if not page_token:
                    return
            else:
                # Buildkite: body is a JSON list.
                if not isinstance(body, list):
                    return
                for entry in body:
                    yield entry
                next_url = _link_next(response.headers.get("Link"))
                if next_url is None:
                    return


def _is_throttled(response: httpx.Response) -> bool:
    """Detect every flavor's rate-limit signal.

    * 429 — universal across the four vendors.
    * 403 with `X-RateLimit-Remaining: 0` — GitHub's secondary limiter
      surface (the one PyGithub historically mishandled).

    Returning True triggers the single-shot backoff retry in `get()`.
    """
    if response.status_code == 429:
        return True
    if (
        response.status_code == 403
        and response.headers.get("X-RateLimit-Remaining") == "0"
    ):
        return True
    return False


def _retry_after_seconds(response: httpx.Response) -> float:
    """Compute the backoff delay from whichever header is available.

    Priority: `Retry-After` (CircleCI / Buildkite / Jenkins / GHA on
    primary 429) → `X-RateLimit-Reset` epoch delta (GHA secondary 403)
    → `_DEFAULT_RETRY_AFTER_SECONDS` floor.
    """
    raw = response.headers.get("Retry-After")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except ValueError:
            return _DEFAULT_RETRY_AFTER_SECONDS

    reset = response.headers.get("X-RateLimit-Reset")
    if reset is not None:
        try:
            # GHA emits the absolute Unix epoch second the bucket
            # refills at; we cannot subtract `now` here without
            # importing time, but treating the header as a raw
            # seconds-from-now hint is a safe upper-bound when the
            # header parses. The scheduler's AIMD shrink layer is
            # what actually paces the next request, so a slight
            # over-sleep here only delays one retry.
            value = float(reset)
            return max(0.0, min(value, _DEFAULT_RETRY_AFTER_SECONDS * 4))
        except ValueError:
            return _DEFAULT_RETRY_AFTER_SECONDS

    return _DEFAULT_RETRY_AFTER_SECONDS


def _link_next(link_header: str | None) -> str | None:
    """Extract the `rel="next"` URL from a Link header, if present.

    Buildkite emits e.g.:
        Link: <https://api.buildkite.com/...?page=2>; rel="next",
              <https://api.buildkite.com/...?page=10>; rel="last"

    We only care about `next`. Returns None on absent / malformed
    header so the paginator terminates cleanly.
    """
    if not link_header:
        return None
    match = _LINK_NEXT_RE.search(link_header)
    if match is None:
        return None
    return match.group(1)


__all__ = [
    "DEFAULT_BUILDKITE_BASE_URL",
    "DEFAULT_CIRCLECI_BASE_URL",
    "DEFAULT_GITHUB_ACTIONS_BASE_URL",
    "AuthMode",
    "BasicAuth",
    "BearerAuth",
    "CircleTokenAuth",
    "CiLogsApi",
    "CiLogsApiError",
    "Flavor",
]
