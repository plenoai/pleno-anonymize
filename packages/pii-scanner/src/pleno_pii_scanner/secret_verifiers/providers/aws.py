"""AWS liveness verifier — STS GetCallerIdentity via SigV4.

We sign requests with stdlib hmac/hashlib instead of pulling boto3 in
just to call one endpoint. The verifier needs the access-key-id (the
detected secret) plus the matching secret-access-key, which the
scanner can supply via VerifyContext.extra["aws_secret_access_key"].
A standalone access-key-id without its secret returns "unknown" —
liveness cannot be probed without both halves.

Optional VerifyContext.extra keys:
  aws_secret_access_key: str  required to actually probe
  aws_session_token:     str  for STS-issued temporary credentials
  aws_region:            str  defaults to "us-east-1"
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac

import httpx

from ..base import VerificationResult, VerifyContext
from ._http import build_client

_SERVICE = "sts"
_HOST_TEMPLATE = "sts.{region}.amazonaws.com"
_BODY = "Action=GetCallerIdentity&Version=2011-06-15"
_BODY_SHA256 = hashlib.sha256(_BODY.encode()).hexdigest()


class AwsVerifier:
    name = "aws"
    entities = frozenset({"AWS_ACCESS_KEY", "AWS_SECRET_KEY"})

    async def verify(
        self, value: str, *, ctx: VerifyContext
    ) -> VerificationResult:
        secret_key = ctx.extra.get("aws_secret_access_key") if ctx.extra else None
        if not isinstance(secret_key, str) or not secret_key:
            return VerificationResult(
                state="unknown",
                detail="missing aws_secret_access_key in VerifyContext.extra",
                ttl_seconds=300,
            )
        session_token = ctx.extra.get("aws_session_token") if ctx.extra else None
        if session_token is not None and not isinstance(session_token, str):
            session_token = None
        region_value = ctx.extra.get("aws_region") if ctx.extra else None
        region = region_value if isinstance(region_value, str) and region_value else "us-east-1"
        now_value = ctx.extra.get("_aws_now") if ctx.extra else None
        now = now_value if isinstance(now_value, _dt.datetime) else _dt.datetime.now(_dt.UTC)

        host = _HOST_TEMPLATE.format(region=region)
        url = f"https://{host}/"
        headers = sigv4_sign_post(
            access_key_id=value,
            secret_access_key=secret_key,
            session_token=session_token,
            region=region,
            host=host,
            body=_BODY,
            now=now,
        )

        async with build_client(ctx) as client:
            try:
                response = await client.post(url, content=_BODY, headers=headers)
            except httpx.TimeoutException:
                return VerificationResult(
                    state="error", detail="timeout", ttl_seconds=60
                )
            except httpx.HTTPError as exc:
                return VerificationResult(
                    state="error",
                    detail=f"transport: {type(exc).__name__}",
                    ttl_seconds=60,
                )
        return _classify(response)


def _classify(response: httpx.Response) -> VerificationResult:
    status = response.status_code
    text = response.text
    if status == 200:
        arn = _between(text, "<Arn>", "</Arn>")
        account = _between(text, "<Account>", "</Account>")
        user_id = _between(text, "<UserId>", "</UserId>")
        metadata: dict[str, object] = {}
        if arn:
            metadata["arn"] = arn
        if account:
            metadata["account"] = account
        if user_id:
            metadata["user_id"] = user_id
        return VerificationResult(
            state="live",
            detail=f"valid credentials for {arn or account or 'aws'}",
            metadata=metadata,
        )
    if status in (401, 403):
        # STS returns 403 for invalid signature / inactive key; the
        # error code lives in the XML body. We expose it (sans secret)
        # in detail so operators can distinguish revoked vs disabled.
        code = _between(text, "<Code>", "</Code>") or "unauthorized"
        return VerificationResult(state="revoked", detail=f"{status} {code}")
    if status == 429:
        return VerificationResult(
            state="rate_limited", detail="aws rate limited", ttl_seconds=60
        )
    if 500 <= status < 600:
        return VerificationResult(
            state="error", detail=f"upstream {status}", ttl_seconds=60
        )
    return VerificationResult(
        state="unknown", detail=f"unexpected {status}", ttl_seconds=300
    )


def _between(text: str, start: str, end: str) -> str | None:
    i = text.find(start)
    if i == -1:
        return None
    j = text.find(end, i + len(start))
    if j == -1:
        return None
    return text[i + len(start) : j]


def sigv4_sign_post(
    *,
    access_key_id: str,
    secret_access_key: str,
    session_token: str | None,
    region: str,
    host: str,
    body: str,
    now: _dt.datetime,
) -> dict[str, str]:
    """Compute SigV4 headers for a POST to https://{host}/ with body.

    Implements the canonical AWS signing scheme so the AWS liveness
    probe does not require boto3. Tested against the AWS docs example
    fixtures (see tests/secret_verifiers/providers/test_aws.py).
    """
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    canonical_uri = "/"
    canonical_query = ""
    payload_hash = hashlib.sha256(body.encode()).hexdigest()
    canonical_headers_dict: dict[str, str] = {
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if session_token:
        canonical_headers_dict["x-amz-security-token"] = session_token

    signed_headers = ";".join(sorted(canonical_headers_dict))
    canonical_headers = "".join(
        f"{name}:{_canonical_header_value(canonical_headers_dict[name])}\n"
        for name in sorted(canonical_headers_dict)
    )
    canonical_request = "\n".join(
        [
            "POST",
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    credential_scope = f"{date_stamp}/{region}/{_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    signing_key = _derive_signing_key(secret_access_key, date_stamp, region, _SERVICE)
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 "
        f"Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    headers = {
        "Authorization": authorization,
        "Content-Type": canonical_headers_dict["content-type"],
        "Host": host,
        "X-Amz-Content-Sha256": payload_hash,
        "X-Amz-Date": amz_date,
    }
    if session_token:
        headers["X-Amz-Security-Token"] = session_token
    return headers


def _canonical_header_value(value: str) -> str:
    # AWS canonicalisation collapses internal whitespace and trims
    # both ends. The simple form is enough for our fixed header set.
    return " ".join(value.split())


def _derive_signing_key(
    secret_access_key: str, date_stamp: str, region: str, service: str
) -> bytes:
    k_date = hmac.new(
        ("AWS4" + secret_access_key).encode(), date_stamp.encode(), hashlib.sha256
    ).digest()
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


