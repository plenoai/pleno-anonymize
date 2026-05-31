#!/usr/bin/env python3
"""Regenerate docs/assets/redact-banner.png — a real before/after of image OCR redaction.

This dogfoods the production redactor: it runs presidio's ImageRedactorEngine
(the exact engine POST /api/redact uses) on docs/assets/demo.webp and composes
the BEFORE | AFTER banner. The AFTER panel is genuine product output — presidio
OCRs the image with Tesseract and blacks out detected PII text (e.g. the
location-revealing URL on the pizza box).

Run:
  PATH="/opt/homebrew/bin:$PATH" uv run --no-project --with pillow \
      --with pytesseract --with presidio-image-redactor \
      python docs/assets/make-redact-banner.py

Exits non-zero if presidio's default OCR redacts nothing (changed pixels ~0),
so a broken OCR path can never silently ship a banner that claims to redact
text while leaving it visible.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DEMO = HERE / "demo.webp"
OUT = HERE / "redact-banner.png"

# pytesseract OCRs by writing the image to a temp file and invoking the
# `tesseract` binary on it. When $TMPDIR points at a path the spawned tesseract
# process cannot read (e.g. a per-process sandbox tmpdir), tesseract fails with
# "fopenReadStream: failed to open" and exits 1. Force temp files into a
# repo-local, child-readable directory so the OCR handoff always works.
_OCR_TMP = REPO / ".tmp_ocr"
_OCR_TMP.mkdir(exist_ok=True)
tempfile.tempdir = str(_OCR_TMP)

import pytesseract  # noqa: E402
from PIL import Image, ImageChops, ImageDraw, ImageFont  # noqa: E402
from presidio_image_redactor import ImageRedactorEngine  # noqa: E402

# pytesseract decodes Tesseract's stderr as UTF-8 to surface warnings; on some
# locales that stderr is not valid UTF-8 and the decode itself raises a
# UnicodeDecodeError that masks the real outcome. Swallow decode failures in the
# error generator so a noisy-but-successful Tesseract still produces OCR.
_orig_get_errors = pytesseract.pytesseract.get_errors


def _safe_get_errors(error_string):  # noqa: ANN001
    try:
        return _orig_get_errors(error_string)
    except Exception:
        return ""


pytesseract.pytesseract.get_errors = _safe_get_errors

ARIAL = "/System/Library/Fonts/Supplemental/Arial.ttf"
ARIAL_B = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
MENLO = "/System/Library/Fonts/Menlo.ttc"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def compose(before: Image.Image, after: Image.Image) -> Image.Image:
    DW = 560
    DH = round(DW * before.height / before.width)
    before = before.resize((DW, DH), Image.LANCZOS)
    after = after.resize((DW, DH), Image.LANCZOS)

    PAD, GAP, HEADER, FOOT, LABEL_H = 40, 120, 150, 64, 34
    W = PAD + DW + GAP + DW + PAD
    DOC_Y = HEADER + LABEL_H
    H = DOC_Y + DH + FOOT

    BG, FG, MUTED = (13, 17, 23), (230, 237, 243), (139, 148, 158)
    RED, GREEN, BLUE = (248, 81, 73), (63, 185, 80), (88, 166, 255)

    canvas = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(canvas)

    d.text((PAD, 44), "pleno", font=font(ARIAL_B, 38), fill=FG)
    w = d.textlength("pleno", font=font(ARIAL_B, 38))
    d.text((PAD + w, 44), "-anonymize", font=font(ARIAL_B, 38), fill=GREEN)
    d.text(
        (PAD, 96),
        "Image redaction — location-revealing text is OCR-detected and blacked out",
        font=font(ARIAL, 19),
        fill=MUTED,
    )

    lx, rx = PAD, PAD + DW + GAP

    def label(x: int, text: str, color: tuple[int, int, int]) -> None:
        cy = HEADER + LABEL_H // 2
        d.ellipse((x, cy - 6, x + 12, cy + 6), fill=color)
        d.text((x + 22, HEADER + 4), text, font=font(ARIAL_B, 18), fill=FG)

    label(lx, "BEFORE  original photo", RED)
    label(rx, "AFTER  text redacted", GREEN)

    def paste_doc(img: Image.Image, x: int) -> None:
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, img.width, img.height), radius=14, fill=255
        )
        canvas.paste(img, (x, DOC_Y), mask)
        d.rounded_rectangle(
            (x, DOC_Y, x + img.width, DOC_Y + img.height),
            radius=14,
            outline=(48, 54, 61),
            width=2,
        )

    paste_doc(before, lx)
    paste_doc(after, rx)

    ay = DOC_Y + DH // 2
    ax0, ax1 = lx + DW + 28, rx - 28
    d.line((ax0, ay, ax1 - 14, ay), fill=BLUE, width=4)
    d.polygon([(ax1, ay), (ax1 - 16, ay - 10), (ax1 - 16, ay + 10)], fill=GREEN)
    cap = "OCR redact"
    cw = d.textlength(cap, font=font(MENLO, 14))
    d.text(((ax0 + ax1) / 2 - cw / 2, ay - 34), cap, font=font(MENLO, 14), fill=BLUE)

    d.text(
        (PAD, H - FOOT + 18),
        "POST /api/redact  ·  image OCR redaction  ·  presidio + Tesseract",
        font=font(MENLO, 14),
        fill=MUTED,
    )
    return canvas


def main() -> None:
    if not DEMO.exists():
        sys.exit(f"missing {DEMO} — place the demo image there first")
    before = Image.open(DEMO).convert("RGB")

    # Genuine product output: presidio's ImageRedactorEngine, default settings.
    after = ImageRedactorEngine().redact(before, fill=(0, 0, 0))

    diff = ImageChops.difference(before, after).convert("L")
    # histogram()[0] is the count of unchanged (value-0) pixels; everything else
    # differs between before and after.
    changed = diff.width * diff.height - diff.histogram()[0]
    print(f"changed pixels: {changed}")
    if changed == 0:
        sys.exit(
            "presidio default OCR redacted nothing on demo.webp — refusing to "
            "ship a banner that claims to redact text while leaving it visible"
        )

    compose(before, after).save(OUT)
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
