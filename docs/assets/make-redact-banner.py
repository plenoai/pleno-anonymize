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
    # Fixed 1280x640 banner — the size GitHub/social cards render best at.
    W, H = 1280, 640
    PAD, GAP = 40, 48
    DW = (W - 2 * PAD - GAP) // 2
    DH = round(DW * before.height / before.width)
    before = before.resize((DW, DH), Image.LANCZOS)
    after = after.resize((DW, DH), Image.LANCZOS)

    BG, FG = (13, 17, 23), (230, 237, 243)
    RED, GREEN, BLUE = (248, 81, 73), (63, 185, 80), (88, 166, 255)

    canvas = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(canvas)

    # Brand wordmark only — small explanatory text is dropped so the photos
    # carry the banner.
    d.text((PAD, 40), "pleno", font=font(ARIAL_B, 46), fill=FG)
    w = d.textlength("pleno", font=font(ARIAL_B, 46))
    d.text((PAD + w, 40), "-anonymize", font=font(ARIAL_B, 46), fill=GREEN)

    lx, rx = PAD, PAD + DW + GAP
    LABEL_Y = 152
    DOC_Y = LABEL_Y + 48

    def label(x: int, text: str, color: tuple[int, int, int]) -> None:
        cy = LABEL_Y + 13
        d.ellipse((x, cy - 9, x + 18, cy + 9), fill=color)
        d.text((x + 30, LABEL_Y), text, font=font(ARIAL_B, 26), fill=FG)

    label(lx, "BEFORE", RED)
    label(rx, "AFTER", GREEN)

    def paste_doc(img: Image.Image, x: int) -> None:
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, img.width, img.height), radius=16, fill=255
        )
        canvas.paste(img, (x, DOC_Y), mask)
        d.rounded_rectangle(
            (x, DOC_Y, x + img.width, DOC_Y + img.height),
            radius=16,
            outline=(48, 54, 61),
            width=2,
        )

    paste_doc(before, lx)
    paste_doc(after, rx)

    ay = DOC_Y + DH // 2
    cx = lx + DW + GAP // 2
    d.line((cx - 18, ay, cx + 8, ay), fill=BLUE, width=5)
    d.polygon([(cx + 22, ay), (cx + 6, ay - 12), (cx + 6, ay + 12)], fill=GREEN)

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
