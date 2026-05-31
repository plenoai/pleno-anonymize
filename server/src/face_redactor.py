"""Face detection and black-box redaction for images.

Presidio's image redactor only blacks out OCR'd PII text; it cannot find
faces. This module adds face redaction on top, using OpenCV's YuNet ONNX
detector with a Haar-cascade fallback.

All heavy imports (cv2, numpy) happen lazily inside functions so that merely
importing this module never fails when OpenCV is absent (e.g. in a test
environment that only exercises the non-face code path).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL import Image

_MODEL_PATH = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"

# Lazily initialised module globals so detectors are created at most once.
_yunet = None  # cv2.FaceDetectorYN
_yunet_failed = False  # remember a failed YuNet load to avoid retrying every call
_haar = None  # cv2.CascadeClassifier


def _get_yunet():
    """Return a lazily-created YuNet detector, or None if unavailable."""
    global _yunet, _yunet_failed
    if _yunet is not None:
        return _yunet
    if _yunet_failed:
        return None
    try:
        import cv2

        if not _MODEL_PATH.exists():
            _yunet_failed = True
            return None
        # Input size is set per image via setInputSize(); (0, 0) is a placeholder.
        _yunet = cv2.FaceDetectorYN_create(
            str(_MODEL_PATH),
            "",
            (0, 0),
            score_threshold=0.6,
            nms_threshold=0.3,
            top_k=5000,
        )
        return _yunet
    except Exception:
        _yunet_failed = True
        return None


def _get_haar():
    """Return a lazily-created Haar cascade detector, or None if unavailable."""
    global _haar
    if _haar is not None:
        return _haar
    try:
        import cv2

        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        clf = cv2.CascadeClassifier(cascade_path)
        if clf.empty():
            return None
        _haar = clf
        return _haar
    except Exception:
        return None


def _detect_yunet(bgr) -> List[Tuple[int, int, int, int]]:
    detector = _get_yunet()
    if detector is None:
        return []
    h, w = bgr.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(bgr)
    if faces is None:
        return []
    boxes: List[Tuple[int, int, int, int]] = []
    for f in faces:
        x, y, bw, bh = f[0], f[1], f[2], f[3]
        boxes.append((int(x), int(y), int(bw), int(bh)))
    return boxes


def _detect_haar(bgr) -> List[Tuple[int, int, int, int]]:
    detector = _get_haar()
    if detector is None:
        return []
    import cv2

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5)
    return [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in faces]


def _expand_box(
    box: Tuple[int, int, int, int],
    expand: float,
    img_w: int,
    img_h: int,
) -> Tuple[int, int, int, int]:
    """Grow a box by `expand` fraction on each side, clamped to image bounds."""
    x, y, w, h = box
    dx = int(round(w * expand))
    dy = int(round(h * expand))
    x0 = max(0, x - dx)
    y0 = max(0, y - dy)
    x1 = min(img_w, x + w + dx)
    y1 = min(img_h, y + h + dy)
    return (x0, y0, x1 - x0, y1 - y0)


def detect_face_boxes(
    img: "Image.Image",
    *,
    expand: float = 0.2,
) -> List[Tuple[int, int, int, int]]:
    """Detect faces and return (expanded) boxes as (x, y, w, h) tuples.

    Tries YuNet first, then falls back to the Haar cascade. Raises RuntimeError
    only if no detector is available at all.
    """
    import numpy as np

    rgb = np.array(img.convert("RGB"))
    bgr = rgb[:, :, ::-1].copy()  # PIL is RGB; OpenCV expects BGR
    img_h, img_w = bgr.shape[:2]

    raw = _detect_yunet(bgr)
    if not raw and _get_yunet() is None:
        raw = _detect_haar(bgr)
        if not raw and _get_haar() is None:
            raise RuntimeError(
                "No face detector available: OpenCV YuNet model and Haar "
                "cascade are both missing or failed to load."
            )

    return [_expand_box(b, expand, img_w, img_h) for b in raw]


def redact_faces(
    img: "Image.Image",
    *,
    fill: Tuple[int, int, int] = (0, 0, 0),
    expand: float = 0.2,
) -> "Image.Image":
    """Return a copy of `img` with every detected face covered by a filled box.

    If no faces are detected, returns an unchanged copy (never errors). Raises
    RuntimeError only when no face detector is available at all.
    """
    from PIL import ImageDraw

    out = img.copy().convert("RGB")
    boxes = detect_face_boxes(img, expand=expand)
    if not boxes:
        return out

    draw = ImageDraw.Draw(out)
    for x, y, w, h in boxes:
        draw.rectangle([x, y, x + w, y + h], fill=fill)
    return out


__all__ = ["redact_faces", "detect_face_boxes"]
