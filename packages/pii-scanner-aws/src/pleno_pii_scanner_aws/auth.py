"""AWS credential resolution + assume-role chain for the S3 connector.

Translates a `pleno_pii_scanner.credentials.profile.CredentialProfile` into
a concrete set of AWS credentials that aioboto3 can consume, walking any
ordered `AssumeRoleHop` chain via STS so a single scan job can fan out
across hundreds of AWS Organizations member accounts.

Design constraints (ADR-0007 §3, §13, anti-requirements):
- Async only. We use `aioboto3.Session()` so STS calls do not block the
  event loop, and so a per-account fan-out can happen with `asyncio.gather`
  rather than a thread pool.
- Never persist tokens. Credentials live in memory inside `AwsCredentials`
  for the lifetime of the in-flight client; nothing is written to disk.
- Re-assume on token expiry. STS short-lived creds default to 1h, max 12h.
  `AwsCredentials.is_expired()` lets the caller refresh proactively before
  a long-running discover/fetch trips a 403.
- No boto3 sync — only `aioboto3` / `aiobotocore`. Sync `boto3.client(...)`
  exists in the SDK but blocks the loop and would defeat the streaming
  ranged GET path in `s3.py`.

The base identity supports the five forms ADR-0007 §3 enumerates:
explicit access_key/secret, EC2 IMDSv2, ECS task role, EKS IRSA, OIDC
web identity. The first delegates to nothing; the rest let aioboto3's
default credential chain resolve them — we only intercept when the
operator wants to override (explicit keys or web-identity token file).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import aioboto3

from pleno_pii_scanner.credentials.profile import (
    AssumeRoleHop,
    CredentialProfile,
)


# WHY 30s: STS token expiry is wall-clock, but our scan workloads can
# burst for tens of seconds between credential checks. Refreshing 30s
# before expiry gives us enough headroom that a 5-10s S3 paginate cannot
# straddle the boundary and 403 mid-request.
_EXPIRY_SAFETY_MARGIN = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class AwsBaseIdentity:
    """Base AWS identity before any AssumeRole hops are applied.

    Exactly one of the four resolution modes is selected:

      * `access_key_id` + `secret_access_key` (+ optional `session_token`)
        — explicit static credentials. Prefer short-lived STS-issued
        sessions; long-lived IAM user keys are a security anti-pattern.

      * `web_identity_token_file` + `role_arn` — OIDC web-identity
        federation. Used by EKS IRSA, GitHub Actions OIDC, and any other
        platform that mints an OIDC JWT and expects us to exchange it via
        `sts:AssumeRoleWithWebIdentity`. The rest of the chain (account
        hops) layers on top of the resulting session.

      * `profile_name` — pull from `~/.aws/credentials` / `~/.aws/config`.
        Useful for local development; should not be used in containers.

      * (none of the above) — let aioboto3 walk its built-in chain
        (env → ~/.aws → IMDSv2 → ECS → EKS). This is the production
        default for workloads running on AWS infrastructure.
    """

    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    web_identity_token_file: str | None = None
    role_arn: str | None = None
    profile_name: str | None = None
    region: str = "us-east-1"

    def __post_init__(self) -> None:
        # Validate the (access_key XOR web_identity XOR profile XOR default)
        # invariant up front so misconfigurations surface at construction
        # rather than buried inside a STS call traceback.
        modes = sum(
            [
                self.access_key_id is not None or self.secret_access_key is not None,
                self.web_identity_token_file is not None or self.role_arn is not None,
                self.profile_name is not None,
            ]
        )
        if modes > 1:
            raise ValueError(
                "AwsBaseIdentity must select at most one of: explicit keys, "
                "web-identity, profile_name (got multiple)"
            )
        if (self.access_key_id is None) != (self.secret_access_key is None):
            raise ValueError(
                "access_key_id and secret_access_key must be provided together"
            )
        if (self.web_identity_token_file is None) != (self.role_arn is None):
            raise ValueError(
                "web_identity_token_file and role_arn must be provided together"
            )


@dataclass(slots=True)
class AwsCredentials:
    """Resolved AWS credential snapshot ready to pass to aioboto3.

    `expires_at` is `None` for non-STS credentials (static IAM user keys,
    or the local `~/.aws` profile chain), in which case `is_expired()`
    always returns False. STS-issued credentials carry the assume-role
    expiry so callers can decide when to re-walk the chain.

    Not frozen because the loader rotates the same instance in-place when
    re-assuming (see `_assume_role_hop`); rotating-in-place rather than
    rebinding lets long-lived `aioboto3.Session` references stay valid.
    """

    access_key_id: str
    secret_access_key: str
    session_token: str | None = None
    expires_at: datetime | None = None
    region: str = "us-east-1"

    def is_expired(self, now: datetime | None = None) -> bool:
        """True when the token is within `_EXPIRY_SAFETY_MARGIN` of expiry."""
        if self.expires_at is None:
            return False
        current = now or datetime.now(UTC)
        return current + _EXPIRY_SAFETY_MARGIN >= self.expires_at

    def to_session_kwargs(self) -> dict[str, Any]:
        """Render the kwargs `aioboto3.Session(...)` accepts."""
        kwargs: dict[str, Any] = {
            "aws_access_key_id": self.access_key_id,
            "aws_secret_access_key": self.secret_access_key,
            "region_name": self.region,
        }
        if self.session_token is not None:
            kwargs["aws_session_token"] = self.session_token
        return kwargs


@dataclass(frozen=True, slots=True)
class AccountSpec:
    """One target AWS account plus the assume-role chain to reach it.

    Lives in the source config (TOML/YAML), not in CredentialBroker —
    the broker resolves the *base* identity, and `AccountSpec` describes
    the cross-account hops a single scan should perform.

    `account_id` is the 12-digit AWS account id used for the BucketKey
    in the rate limiter; carrying it explicitly avoids a per-account
    `sts:GetCallerIdentity` round-trip just to learn what we already know.
    """

    account_id: str
    chain: tuple[AssumeRoleHop, ...] = ()
    region: str | None = None
    label: str = ""

    def resolved_label(self) -> str:
        return self.label or f"aws:{self.account_id}"


@dataclass(slots=True)
class AwsSessionFactory:
    """Builds aioboto3 Sessions, walking AssumeRole chains as needed.

    A single instance is shared across all S3 operations of one connector
    so the assume-role result is cached per `AccountSpec`. The cache is
    keyed by `account_id`; a re-assume happens transparently when the
    cached `AwsCredentials.is_expired()`.
    """

    base: AwsBaseIdentity
    profile: CredentialProfile | None = None
    _cache: dict[str, AwsCredentials] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Test seam: replace with a stub that returns canned AwsCredentials so
    # tests can exercise the cache + expiry logic without standing up a
    # real STS endpoint via moto.
    _hop_runner: "HopRunner | None" = None

    def base_session(self) -> aioboto3.Session:
        """Construct the un-assumed base aioboto3.Session.

        For explicit keys we pass them directly; for everything else we
        let aioboto3's built-in chain walk (env → ~/.aws → IMDSv2 → ECS
        → EKS), which is the production default on AWS infrastructure.
        """
        if self.base.access_key_id and self.base.secret_access_key:
            kwargs: dict[str, Any] = {
                "aws_access_key_id": self.base.access_key_id,
                "aws_secret_access_key": self.base.secret_access_key,
                "region_name": self.base.region,
            }
            if self.base.session_token is not None:
                kwargs["aws_session_token"] = self.base.session_token
            return aioboto3.Session(**kwargs)
        if self.base.profile_name is not None:
            return aioboto3.Session(
                profile_name=self.base.profile_name, region_name=self.base.region
            )
        # Default chain: aioboto3 will use env / ~/.aws / IMDSv2 / ECS / EKS.
        return aioboto3.Session(region_name=self.base.region)

    async def credentials_for(self, account: AccountSpec) -> AwsCredentials:
        """Resolve credentials for `account`, applying its hop chain.

        Cached per account_id; refreshed when the cached creds are within
        the safety margin of expiry. Two coroutines racing for the same
        account share one in-flight refresh via `_lock`.
        """
        async with self._lock:
            cached = self._cache.get(account.account_id)
            if cached is not None and not cached.is_expired():
                return cached
            creds = await self._resolve(account)
            self._cache[account.account_id] = creds
            return creds

    async def _resolve(self, account: AccountSpec) -> AwsCredentials:
        # WHY ordered chain: AWS Organizations cross-account flows often
        # require a transit role (e.g. trust-anchor in the Org master,
        # then OrganizationAccountAccessRole in each member). Each hop
        # consumes the previous hop's session.
        runner = self._hop_runner or _RealHopRunner()
        region = account.region or self.base.region

        # Seed: the base session, optionally already with a profile-level
        # AssumeRole chain (CredentialProfile.chain). Per-account hops
        # apply on top of that, so an Org master profile + per-account
        # OrganizationAccountAccessRole hop just works.
        seed = await runner.start(self.base_session(), region)

        chain: list[AssumeRoleHop] = []
        if self.profile is not None:
            chain.extend(self.profile.chain)
        chain.extend(account.chain)
        for hop in chain:
            seed = await runner.assume(seed, hop, region)
        return seed


class HopRunner:
    """Strategy for running STS AssumeRole hops.

    Two implementations: `_RealHopRunner` calls `sts:AssumeRole` for real
    via aioboto3, `StubHopRunner` (used in tests) returns canned results.
    Splitting the strategy keeps the cache + chain orchestration
    independently testable from the AWS API surface.
    """

    async def start(
        self, session: aioboto3.Session, region: str
    ) -> AwsCredentials:  # pragma: no cover - abstract
        raise NotImplementedError

    async def assume(
        self, prev: AwsCredentials, hop: AssumeRoleHop, region: str
    ) -> AwsCredentials:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass(slots=True)
class _RealHopRunner(HopRunner):
    """Production HopRunner: uses STS via aioboto3."""

    async def start(
        self, session: aioboto3.Session, region: str
    ) -> AwsCredentials:
        # Materialize the base session into AwsCredentials so the chain
        # walker can treat the seed and hop results uniformly. For the
        # default chain (no explicit keys) aioboto3 may resolve creds
        # asynchronously via IMDSv2 / IRSA; calling get_credentials() on
        # the underlying botocore session is the canonical way to flush.
        botocore_session = session._session  # internal but stable
        creds = await asyncio.to_thread(botocore_session.get_credentials)
        if creds is None:  # pragma: no cover - exercised in moto integration
            raise RuntimeError(
                "aioboto3 returned no credentials; check IAM role / env vars"
            )
        frozen = await asyncio.to_thread(creds.get_frozen_credentials)
        return AwsCredentials(
            access_key_id=frozen.access_key,
            secret_access_key=frozen.secret_key,
            session_token=frozen.token,
            region=region,
        )

    async def assume(
        self, prev: AwsCredentials, hop: AssumeRoleHop, region: str
    ) -> AwsCredentials:
        # WHY new Session per hop: aioboto3 STS clients are bound to the
        # session credentials at construction. To assume role using the
        # *previous* hop's short-lived creds we must build a fresh
        # session from those creds; reusing the base session would still
        # call STS as the master account.
        if hop.provider != "aws":
            raise ValueError(
                f"AwsSessionFactory only handles provider='aws' hops, "
                f"got provider={hop.provider!r}"
            )
        session = aioboto3.Session(**prev.to_session_kwargs())
        params: dict[str, Any] = {
            "RoleArn": hop.role_arn_or_id,
            "RoleSessionName": hop.session_name,
            "DurationSeconds": hop.duration_seconds,
        }
        if hop.external_id is not None:
            params["ExternalId"] = hop.external_id
        async with session.client("sts", region_name=region) as sts:
            result = await sts.assume_role(**params)
        c = result["Credentials"]
        expires = c["Expiration"]
        if isinstance(expires, str):
            # Some stubs return ISO strings; AWS itself returns datetime.
            expires = datetime.fromisoformat(expires)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return AwsCredentials(
            access_key_id=c["AccessKeyId"],
            secret_access_key=c["SecretAccessKey"],
            session_token=c["SessionToken"],
            expires_at=expires,
            region=region,
        )


@dataclass(slots=True)
class StubHopRunner(HopRunner):
    """In-memory HopRunner for tests.

    `seed` is returned by `start()`. Each `assume()` call consumes the
    next entry in `hops` (a list of pre-baked AwsCredentials) so tests
    can inject expiry / role chains deterministically without standing
    up moto's STS endpoint.
    """

    seed: AwsCredentials
    hops: list[AwsCredentials] = field(default_factory=list)
    calls: list[tuple[AssumeRoleHop, str]] = field(default_factory=list)

    async def start(
        self, session: aioboto3.Session, region: str
    ) -> AwsCredentials:
        del session, region
        return self.seed

    async def assume(
        self, prev: AwsCredentials, hop: AssumeRoleHop, region: str
    ) -> AwsCredentials:
        del prev
        if not self.hops:
            raise AssertionError(
                "StubHopRunner ran out of hop responses; test fixture under-baked"
            )
        self.calls.append((hop, region))
        return self.hops.pop(0)


__all__ = [
    "AccountSpec",
    "AwsBaseIdentity",
    "AwsCredentials",
    "AwsSessionFactory",
    "HopRunner",
    "StubHopRunner",
]
