import base64
import io

from fastapi.testclient import TestClient
from PIL import Image

import src.app as app_module
from src.face_redactor import redact_faces

client = TestClient(app_module.app)


def _png_data_url(width: int = 32, height: int = 32) -> str:
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def test_redact_faces_blank_image_unchanged():
    """A blank white image has no faces; redaction returns a same-size RGB copy
    that is (nearly) unchanged."""
    img = Image.new("RGB", (200, 200), color=(255, 255, 255))
    out = redact_faces(img, fill=(0, 0, 0))

    assert isinstance(out, Image.Image)
    assert out.size == img.size
    assert out.mode == "RGB"
    # No faces => no black box drawn => image stays white.
    colors = out.getcolors()
    assert colors is not None
    assert colors == [(200 * 200, (255, 255, 255))]


def test_redact_faces_api_skips_ocr():
    """POST /api/redact with redact_faces=true must return 200 with a data URL,
    even though Tesseract is absent, because the face path never touches OCR."""
    resp = client.post(
        "/api/redact",
        json={"image": _png_data_url(), "redact_faces": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "image" in body
    assert body["image"].startswith("data:image/")
