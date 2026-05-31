#!/usr/bin/env python3
"""Regenerate docs/assets/redact-banner.png — a real before/after of FACE redaction.

This dogfoods the production detector: it imports the same `face_redactor`
module the server's POST /api/redact uses (redact_faces=True), runs it on
docs/assets/demo.webp, and composes the BEFORE | AFTER banner. The AFTER panel
is genuine detector output — faces are located with OpenCV YuNet and filled.

Run:
  uv run --no-project --with opencv-python-headless --with pillow --with numpy \
      python docs/assets/make-redact-banner.py

Exits non-zero if no face is detected, so a broken detector can never silently
ship a banner that claims to redact faces while leaving them visible.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SRC = REPO / "demo.webp"  # overridden below
DEMO = HERE / "demo.webp"
OUT = HERE / "redact-banner.png"

# import the production face redactor (server/src/face_redactor.py)
sys.path.insert(0, str(REPO / "server" / "src"))
import face_redactor  # noqa: E402

ARIAL = "/System/Library/Fonts/Supplemental/Arial.ttf"
ARIAL_B = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
MENLO = "/System/Library/Fonts/Menlo.ttc"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def compose(before: Image.Image, after: Image.Image, n_faces: int) -> Image.Image:
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
        "Image redaction — faces are detected and boxed before the photo leaves your boundary",
        font=font(ARIAL, 19),
        fill=MUTED,
    )

    lx, rx = PAD, PAD + DW + GAP

    def label(x: int, text: str, color: tuple[int, int, int]) -> None:
        cy = HEADER + LABEL_H // 2
        d.ellipse((x, cy - 6, x + 12, cy + 6), fill=color)
        d.text((x + 22, HEADER + 4), text, font=font(ARIAL_B, 18), fill=FG)

    label(lx, "BEFORE  original photo", RED)
    label(rx, f"AFTER  {n_faces} face(s) redacted", GREEN)

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
    cap = "redact_faces"
    cw = d.textlength(cap, font=font(MENLO, 14))
    d.text(((ax0 + ax1) / 2 - cw / 2, ay - 34), cap, font=font(MENLO, 14), fill=BLUE)

    d.text(
        (PAD, H - FOOT + 18),
        'POST /api/redact  ·  {"redact_faces": true}  ·  OpenCV YuNet face detector',
        font=font(MENLO, 14),
        fill=MUTED,
    )
    return canvas


def main() -> None:
    if not DEMO.exists():
        sys.exit(f"missing {DEMO} — place the demo image there first")
    before = Image.open(DEMO).convert("RGB")
    boxes = face_redactor.detect_face_boxes(before)
    if not boxes:
        sys.exit("no faces detected in demo.webp — refusing to ship a face-redaction banner")
    after = face_redactor.redact_faces(before, fill=(0, 0, 0))
    compose(before, after, len(boxes)).save(OUT)
    print(f"detected {len(boxes)} face(s): {boxes}")
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
