"""Remote engine — HTTP client for a hosted pleno-anonymize server."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterable

from ._engine import Finding, RedactResult


class PlenoAnonymizeError(RuntimeError):
    """Raised when the remote engine fails (HTTP non-2xx, timeout, transport)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class RemoteEngine:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
        user_agent: str = "pleno-anonymize-sdk-py",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.user_agent = user_agent

    def analyze(
        self,
        text: str,
        *,
        language: str = "ja",
        entities: Iterable[str] | None = None,
    ) -> list[Finding]:
        payload: dict[str, Any] = {"text": text, "language": language}
        if entities is not None:
            payload["entities"] = list(entities)
        data = self._post("/api/analyze", payload)
        if not isinstance(data, list):
            raise PlenoAnonymizeError(
                "unexpected analyze response (not a list)", body=data
            )
        return [
            Finding(
                entity_type=str(item["entity_type"]),
                start=int(item["start"]),
                end=int(item["end"]),
                score=float(item["score"]),
                text=str(item.get("text", text[int(item["start"]) : int(item["end"])])),
            )
            for item in data
        ]

    def redact(
        self,
        text: str,
        *,
        language: str = "ja",
        entities: Iterable[str] | None = None,
        operators: dict[str, dict[str, object]] | None = None,
    ) -> RedactResult:
        payload: dict[str, Any] = {"text": text, "language": language}
        if entities is not None:
            payload["entities"] = list(entities)
        if operators is not None:
            payload["operators"] = operators
        data = self._post("/api/redact", payload)
        if not isinstance(data, dict):
            raise PlenoAnonymizeError(
                "unexpected redact response (not an object)", body=data
            )
        return RedactResult(text=str(data.get("text", "")))

    def health(self) -> dict[str, Any]:
        result = self._get("/health")
        if not isinstance(result, dict):
            return {"status": "ok"}
        return result

    # internal -------------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request(path, payload=payload)

    def _get(self, path: str) -> Any:
        return self._request(path, payload=None)

    def _request(self, path: str, *, payload: dict[str, Any] | None) -> Any:
        url = f"{self.base_url}{path}"
        headers = {
            "accept": "application/json",
            "user-agent": self.user_agent,
        }
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        body_bytes: bytes | None = None
        if payload is not None:
            headers["content-type"] = "application/json"
            body_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers=headers,
            method="POST" if payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = None
            raise PlenoAnonymizeError(
                f"pleno-anonymize {req.get_method()} {path} failed: {e.code} {e.reason}",
                status=e.code,
                body=body,
            ) from e
        except urllib.error.URLError as e:
            raise PlenoAnonymizeError(
                f"pleno-anonymize {req.get_method()} {path} request failed: {e.reason}"
            ) from e
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
