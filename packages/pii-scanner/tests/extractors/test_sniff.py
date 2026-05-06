"""Tests for the magic-byte MIME sniffer."""

from __future__ import annotations

import gzip
import io
import zipfile

from pleno_pii_scanner.extractors.sniff import OCTET_STREAM, sniff


class TestSniffMagic:
    def test_empty_is_octet_stream(self) -> None:
        assert sniff(b"") == OCTET_STREAM

    def test_pdf(self) -> None:
        assert sniff(b"%PDF-1.7\nfake content") == "application/pdf"

    def test_gzip(self) -> None:
        assert sniff(gzip.compress(b"x")) == "application/gzip"

    def test_zstd(self) -> None:
        assert sniff(b"\x28\xb5\x2f\xfd\x00\x00") == "application/zstd"

    def test_7z(self) -> None:
        assert sniff(b"7z\xbc\xaf\x27\x1c\x00") == "application/x-7z-compressed"

    def test_bzip2(self) -> None:
        assert sniff(b"BZh91AY") == "application/x-bzip2"

    def test_xz(self) -> None:
        assert sniff(b"\xfd7zXZ\x00\x00") == "application/x-xz"

    def test_parquet(self) -> None:
        assert sniff(b"PAR1\x00\x00") == "application/vnd.apache.parquet"

    def test_orc(self) -> None:
        assert sniff(b"ORC\x00\x00") == "application/vnd.apache.orc"

    def test_avro(self) -> None:
        assert sniff(b"Obj\x01\x00") == "application/vnd.apache.avro"

    def test_zip_generic(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.txt", b"x")
        assert sniff(buf.getvalue()) == "application/zip"

    def test_zip_empty_eocd(self) -> None:
        # PK\x05\x06 = end-of-central-directory; an empty zip starts here.
        assert sniff(b"PK\x05\x06\x00\x00\x00\x00\x00\x00\x00\x00") == "application/zip"

    def test_docx(self) -> None:
        # Synthetic OOXML zip — fastest way without writing a real docx.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", b"<types/>")
            zf.writestr("word/document.xml", b"<doc/>")
        assert "wordprocessingml" in sniff(buf.getvalue())

    def test_xlsx(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", b"<types/>")
            zf.writestr("xl/workbook.xml", b"<wb/>")
        assert "spreadsheetml" in sniff(buf.getvalue())

    def test_pptx(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", b"<types/>")
            zf.writestr("ppt/presentation.xml", b"<p/>")
        assert "presentationml" in sniff(buf.getvalue())

    def test_tar(self) -> None:
        # Construct a minimal tar header where bytes 257..262 are "ustar".
        header = b"\x00" * 257 + b"ustar" + b"\x00" * 200
        assert sniff(header) == "application/x-tar"

    def test_html_doctype(self) -> None:
        assert sniff(b"<!DOCTYPE html>\n<html>") == "text/html"

    def test_html_bare(self) -> None:
        assert sniff(b"<html><body>x</body></html>") == "text/html"

    def test_text_plain(self) -> None:
        assert sniff(b"hello world\nthis is a text file\n") == "text/plain"

    def test_text_with_nul_is_binary(self) -> None:
        # A NUL inside the surface area means binary -> octet-stream.
        assert sniff(b"hello\x00world") == OCTET_STREAM

    def test_high_control_ratio_is_binary(self) -> None:
        # >30% control chars -> not text-likely.
        assert sniff(b"\x01\x02\x03\x04\x05\x06\x07hello") == OCTET_STREAM

    def test_japanese_utf8_is_text(self) -> None:
        # Multi-byte UTF-8 must not trip the "binary" heuristic.
        assert sniff("こんにちは世界これはテキストです".encode()) == "text/plain"

    def test_short_tar_not_misclassified(self) -> None:
        # Less than 265 bytes can't carry the ustar marker — must not
        # report tar.
        assert sniff(b"x" * 100) != "application/x-tar"

    def test_opendocument(self) -> None:
        # ODF: a zip whose first member is "mimetype" carrying an
        # opendocument MIME string, stored uncompressed at offset 30.
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zi = zipfile.ZipInfo("mimetype")
            zi.compress_type = zipfile.ZIP_STORED
            zf.writestr(zi, b"application/vnd.oasis.opendocument.text")
        out = sniff(buf.getvalue())
        assert "opendocument" in out

    def test_is_text_likely_empty_sample(self) -> None:
        # Internal: an exactly-zero-length surface area must short
        # circuit to "not text" rather than divide by zero.
        from pleno_pii_scanner.extractors.sniff import _is_text_likely

        assert _is_text_likely(b"") is False
