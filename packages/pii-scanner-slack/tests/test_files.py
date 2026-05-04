"""Tests for `_files`: text/binary heuristic + Bearer download."""

from __future__ import annotations

import httpx
import pytest

from pleno_pii_scanner_slack import _files


class TestIsTextLike:
    def test_text_mimetype(self) -> None:
        assert _files.is_text_like("text/plain", "x") is True

    def test_application_json(self) -> None:
        assert _files.is_text_like("application/json", None) is True

    def test_application_yaml(self) -> None:
        assert _files.is_text_like("application/x-yaml", None) is True
        assert _files.is_text_like("application/yaml", None) is True

    def test_application_xml(self) -> None:
        assert _files.is_text_like("application/xml", None) is True

    def test_application_x_sh(self) -> None:
        assert _files.is_text_like("application/x-sh", None) is True

    def test_extension_python(self) -> None:
        assert _files.is_text_like(None, "main.py") is True

    def test_extension_uppercase(self) -> None:
        assert _files.is_text_like(None, "main.PY") is True

    def test_dockerfile_no_extension(self) -> None:
        assert _files.is_text_like(None, "Dockerfile") is True

    def test_multiple_dots(self) -> None:
        assert _files.is_text_like(None, "schema.gen.py") is True

    def test_binary_mimetype(self) -> None:
        assert _files.is_text_like("image/png", "logo.png") is False

    def test_unknown_extension(self) -> None:
        assert _files.is_text_like(None, "blob.bin") is False

    def test_no_signal(self) -> None:
        assert _files.is_text_like(None, None) is False

    def test_no_extension_arbitrary(self) -> None:
        # File with no dot and no special name -> not text-like.
        assert _files.is_text_like(None, "README") is False


class TestFileBodyInvariant:
    def test_text_only(self) -> None:
        body = _files.FileBody(text="hi")
        assert body.text == "hi"
        assert body.binary is None

    def test_binary_only(self) -> None:
        body = _files.FileBody(binary=b"\x00\x01")
        assert body.binary == b"\x00\x01"

    def test_both_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            _files.FileBody(text="x", binary=b"y")

    def test_neither_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            _files.FileBody()


class TestDownloadFile:
    async def test_text_path(self) -> None:
        # MockTransport lets us assert the Bearer header without spinning
        # up an aiohttp test server.
        seen_auth: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth["v"] = request.headers.get("Authorization", "")
            return httpx.Response(
                200,
                content=b"hello",
                headers={"Content-Type": "text/plain"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            body = await _files.download_file(
                client=client,
                token="xoxb-zzz",
                url="https://files.slack.com/abc",
                mimetype="text/plain",
                name="foo.txt",
            )
        assert body.text == "hello"
        assert body.binary is None
        assert body.content_type.startswith("text/plain")
        assert seen_auth["v"] == "Bearer xoxb-zzz"

    async def test_binary_path(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            return httpx.Response(
                200,
                content=b"\x89PNGdata",
                headers={"Content-Type": "image/png"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            body = await _files.download_file(
                client=client,
                token="xoxb-zzz",
                url="https://files.slack.com/img",
                mimetype="image/png",
                name="x.png",
            )
        assert body.binary == b"\x89PNGdata"
        assert body.text is None

    async def test_text_via_content_type_header(self) -> None:
        # Mimetype hint says binary, but the response Content-Type is text:
        # the response wins so we still go through the text branch.
        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            return httpx.Response(
                200,
                content=b"hi",
                headers={"Content-Type": "text/markdown"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            body = await _files.download_file(
                client=client,
                token="xoxp-q",
                url="https://files.slack.com/x",
                mimetype="application/octet-stream",
                name="x.bin",
            )
        assert body.text == "hi"

    async def test_decode_replaces_invalid_utf8(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            return httpx.Response(
                200,
                content=b"\xff\xfeOK",
                headers={"Content-Type": "text/plain"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            body = await _files.download_file(
                client=client,
                token="xoxb-z",
                url="https://files.slack.com/x",
                mimetype="text/plain",
                name="x.txt",
            )
        assert "OK" in (body.text or "")

    async def test_http_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await _files.download_file(
                    client=client,
                    token="x",
                    url="https://files.slack.com/x",
                    mimetype=None,
                    name=None,
                )
