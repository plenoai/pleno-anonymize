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
    PAD, GAP = 32, 36
    # The banner is 2:1 but the source photo is 1.5:1, so two side-by-side
    # panels at full aspect leave a ~160px vertical void. Center-crop each photo
    # to a slightly taller 1.26:1 so the panels fill the height — the pizza box
    # and its text sit dead-center and are fully preserved by the crop.
    AR = 1.26
    DW = (W - 2 * PAD - GAP) // 2
    DH = round(DW / AR)

    def fit(img: Image.Image) -> Image.Image:
        crop_w = round(img.height * AR)
        left = (img.width - crop_w) // 2
        return img.crop((left, 0, left + crop_w, img.height)).resize(
            (DW, DH), Image.LANCZOS
        )

    before, after = fit(before), fit(after)

    BG, FG = (13, 17, 23), (230, 237, 243)
    RED, GREEN, BLUE = (248, 81, 73), (63, 185, 80), (88, 166, 255)

    canvas = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(canvas)

    # Left-aligned brand wordmark. Small explanatory text is dropped so the
    # photos carry the banner.
    f_title = font(ARIAL_B, 44)
    ty = 34
    d.text((PAD, ty), "pleno", font=f_title, fill=FG)
    w1 = d.textlength("pleno", font=f_title)
    d.text((PAD + w1, ty), "-anonymize", font=f_title, fill=GREEN)

    lx, rx = PAD, PAD + DW + GAP
    # Sit the panels just below the wordmark — a tight, deliberate gap rather
    # than the large void left by removing the sub-headline.
    DOC_Y = ty + 44 + 26

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

    # BEFORE / AFTER as overlay chips inside each panel — no separate label row.
    def chip(x: int, text: str, color: tuple[int, int, int]) -> None:
        f = font(ARIAL_B, 20)
        tw = d.textlength(text, font=f)
        px, py, pdx, pdy = x + 14, DOC_Y + 14, 12, 7
        d.rounded_rectangle(
            (px, py, px + tw + 2 * pdx, py + 20 + 2 * pdy), radius=9, fill=color
        )
        d.text((px + pdx, py + pdy), text, font=f, fill=BG)

    chip(lx, "BEFORE", RED)
    chip(rx, "AFTER", GREEN)

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
