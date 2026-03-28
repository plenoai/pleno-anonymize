import os
import json
import base64
import io
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
import httpx
from fastapi import FastAPI, Request, Response
from presidio_anonymizer.entities import OperatorConfig
from pydantic import BaseModel
from scalar_fastapi import get_scalar_api_reference
from PIL import Image

# Lazy initialization for cold start optimization
_nlp_ja = None
_nlp_en = None
_analyzer = None
_anonymizer = None
_image_redactor = None


def _init_presidio():
    """Lazy initialization of Presidio components."""
    global _nlp_ja, _nlp_en, _analyzer, _anonymizer, _image_redactor

    if _analyzer is not None:
        return

    import spacy
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import SpacyNlpEngine
    from presidio_anonymizer import AnonymizerEngine
    from server.recognizers_ja import ALL_JA_RECOGNIZERS

    class MultiLangSpacyNlpEngine(SpacyNlpEngine):
        def __init__(self, models: dict):
            super().__init__()
            self.nlp = models

    # Load Japanese NER model from packages/models/ja_ner_ja-0.1.0
    app_dir = Path(__file__).parent
    model_path = (
        app_dir.parent
        / "packages"
        / "models"
        / "ja_ner_ja-0.1.0"
        / "ja_ner_ja"
        / "ja_ner_ja-0.1.0"
    )
    _nlp_ja = spacy.load(str(model_path))

    # Try to load English model, fallback to None if not available
    try:
        _nlp_en = spacy.load("en_core_web_sm")
    except OSError:
        _nlp_en = None

    # Build models dict with available models
    models = {"ja": _nlp_ja}
    if _nlp_en is not None:
        models["en"] = _nlp_en

    engine = MultiLangSpacyNlpEngine(models)
    _analyzer = AnalyzerEngine(nlp_engine=engine)

    for recognizer in ALL_JA_RECOGNIZERS:
        _analyzer.registry.add_recognizer(recognizer)

    _anonymizer = AnonymizerEngine()


def get_analyzer():
    _init_presidio()
    return _analyzer


def get_anonymizer():
    _init_presidio()
    return _anonymizer


def get_image_redactor():
    global _image_redactor
    if _image_redactor is None:
        from presidio_image_redactor import ImageRedactorEngine, ImageAnalyzerEngine

        image_analyzer = ImageAnalyzerEngine(analyzer_engine=get_analyzer())
        _image_redactor = ImageRedactorEngine(image_analyzer_engine=image_analyzer)
    return _image_redactor


app = FastAPI(
    title="pleno-anonymize",
    description="""
PII (Personal Information) anonymization server with Japanese support.

## Features
- **Text PII Detection** - Detect personal information in text using custom Japanese NER model + Presidio
- **Text PII Redaction** - Anonymize personal information by replacing with placeholders
- **Image PII Redaction** - Redact PII from images using OCR
- **API Proxies** - Transparent proxies to OpenAI, Anthropic, and Gemini APIs with automatic PII redaction

## Supported Entity Types (Japanese)
### NER Model
- `PERSON` - Person names (人名)
- `ADDRESS` - Addresses (住所)
- `ORGANIZATION` - Organization names (組織名)
- `DATE_OF_BIRTH` - Date of birth (生��月日)
- `BANK_ACCOUNT` - Bank account info (銀行口座)

### Pattern-based
- `EMAIL_ADDRESS` - Email addresses
- `PHONE_NUMBER` - Phone numbers (全角/半角)
- `MY_NUMBER` - My Number (マイナンバ���)
- `CREDIT_CARD` - Credit card numbers
- `PASSPORT` - Passport numbers
- `DRIVER_LICENSE` - Driver license numbers
- `IP_ADDRESS` - IP addresses

## API Proxy Endpoints
All proxy endpoints automatically redact PII before sending to the upstream API,
and restore original values in the response.

| Endpoint | Upstream API |
|----------|-------------|
| `/api/openai/{path}` | OpenAI (Chat Completions & Responses API) |
| `/api/anthropic/{path}` | Anthropic Messages API |
| `/api/gemini/{path}` | Google Gemini API |
""",
    version="1.0.0",
    contact={
        "name": "pleno-anonymize",
        "url": "https://github.com/HikaruEgashira/pleno-anonymize",
    },
    docs_url=None,  # Disable default Swagger UI
    redoc_url=None,  # Disable ReDoc
)


@app.get("/docs", include_in_schema=False)
async def scalar_docs():
    """Scalar API Reference"""
    return get_scalar_api_reference(
        openapi_url=app.openapi_url,
        title="pleno-anonymize API",
    )


class AnalyzeRequest(BaseModel):
    text: str
    language: str = "ja"
    entities: Optional[List[str]] = None


class RedactRequest(BaseModel):
    text: Optional[str] = None
    image: Optional[str] = None  # base64 encoded image or data URL
    language: str = "ja"
    entities: Optional[List[str]] = None
    operators: Optional[Dict[str, Dict[str, Any]]] = None
    fill_color: Optional[List[int]] = [0, 0, 0]  # RGB for image redaction


@app.post("/api/analyze", tags=["PII Detection"])
async def analyze(req: AnalyzeRequest):
    """
    Detect PII entities in text.

    Analyzes the input text and returns a list of detected PII entities
    with their positions and confidence scores.

    **Example Request:**
    ```json
    {
      "text": "My name is John Doe and my email is john@example.com",
      "language": "en"
    }
    ```

    **Example Response:**
    ```json
    [
      {"entity_type": "PERSON", "start": 11, "end": 19, "score": 0.85, "text": "John Doe"},
      {"entity_type": "EMAIL_ADDRESS", "start": 38, "end": 54, "score": 1.0, "text": "john@example.com"}
    ]
    ```
    """
    results = get_analyzer().analyze(
        text=req.text, language=req.language, entities=req.entities
    )
    return [
        {
            "entity_type": r.entity_type,
            "start": r.start,
            "end": r.end,
            "score": r.score,
            "text": req.text[r.start : r.end],
        }
        for r in results
    ]


@app.post("/api/redact", tags=["PII Redaction"])
async def redact(req: RedactRequest):
    """
    Redact (anonymize) PII in text and/or images.

    Replaces detected PII with placeholders like `<PERSON>`, `<EMAIL_ADDRESS>`.
    Supports both text and image redaction.

    **Text Redaction Example:**
    ```json
    {
      "text": "Contact John at john@example.com"
    }
    ```
    Response:
    ```json
    {
      "text": "Contact <PERSON> at <EMAIL_ADDRESS>",
      "items": ["replace", "replace"]
    }
    ```

    **Image Redaction:**
    Send a base64-encoded image or data URL in the `image` field.
    The response will contain a redacted image with PII blacked out.
    """
    if not req.text and not req.image:
        return {"error": "Either 'text' or 'image' must be provided"}

    result = {}

    if req.text:
        results = get_analyzer().analyze(
            text=req.text, language=req.language, entities=req.entities
        )
        anonymizers = {}
        for r in results:
            et = r.entity_type
            if et not in anonymizers:
                cfg = req.operators.get(et) if req.operators else None
                if not cfg:
                    anonymizers[et] = OperatorConfig(
                        "replace", {"new_value": f"<{et}>"}
                    )
                else:
                    operator_name = cfg.get("type", "replace")
                    params = {k: v for k, v in cfg.items() if k != "type"}
                    anonymizers[et] = OperatorConfig(operator_name, params)
        out = get_anonymizer().anonymize(
            text=req.text, analyzer_results=results, operators=anonymizers
        )
        result["text"] = out.text
        result["items"] = [it.operator for it in out.items]

    if req.image:
        image_data = req.image
        if image_data.startswith("data:"):
            header, data = image_data.split(",", 1)
            image_bytes = base64.b64decode(data)
            mime_type = (
                header.split(";")[0].split(":")[1] if ":" in header else "image/png"
            )
        else:
            image_bytes = base64.b64decode(image_data)
            mime_type = "image/png"

        img = Image.open(io.BytesIO(image_bytes))
        fill = tuple(req.fill_color) if req.fill_color else (0, 0, 0)
        redacted_img = get_image_redactor().redact(img, fill=fill)

        output_buffer = io.BytesIO()
        fmt = "PNG"
        if "jpeg" in mime_type or "jpg" in mime_type:
            fmt = "JPEG"
        elif "webp" in mime_type:
            fmt = "WEBP"

        redacted_img.save(output_buffer, format=fmt)
        output_buffer.seek(0)
        encoded = base64.b64encode(output_buffer.read()).decode("utf-8")
        result["image"] = f"data:{mime_type};base64,{encoded}"

    return result


OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com")
ANTHROPIC_API_BASE = os.getenv("ANTHROPIC_API_BASE", "https://api.anthropic.com")
GEMINI_API_BASE = os.getenv(
    "GEMINI_API_BASE", "https://generativelanguage.googleapis.com"
)


def redact_text_with_mapping(
    text: str, language: str = "en"
) -> Tuple[str, Dict[str, str]]:
    """Redact PII from text and return mapping for de-anonymization."""
    results = get_analyzer().analyze(text=text, language=language)

    # Sort by start position descending to replace from end
    results_sorted = sorted(results, key=lambda r: r.start, reverse=True)

    mapping = {}
    redacted_text = text

    for r in results_sorted:
        original = text[r.start : r.end]
        placeholder = f"<{r.entity_type}_{r.start}>"
        mapping[placeholder] = original
        redacted_text = redacted_text[: r.start] + placeholder + redacted_text[r.end :]

    return redacted_text, mapping


def deanonymize_text(text: str, mapping: Dict[str, str]) -> str:
    """Restore original values from placeholders."""
    result = text
    for placeholder, original in mapping.items():
        result = result.replace(placeholder, original)
    return result


async def redact_image(image_url: str, http_client: httpx.AsyncClient) -> str:
    if image_url.startswith("data:"):
        header, data = image_url.split(",", 1)
        image_bytes = base64.b64decode(data)
        mime_type = header.split(";")[0].split(":")[1] if ":" in header else "image/png"
    else:
        response = await http_client.get(image_url)
        response.raise_for_status()
        image_bytes = response.content
        content_type = response.headers.get("content-type", "image/png")
        mime_type = content_type.split(";")[0]

    image = Image.open(io.BytesIO(image_bytes))
    redacted_image = get_image_redactor().redact(image, fill=(0, 0, 0))

    output_buffer = io.BytesIO()
    fmt = "PNG"
    if "jpeg" in mime_type or "jpg" in mime_type:
        fmt = "JPEG"
    elif "webp" in mime_type:
        fmt = "WEBP"
    elif "gif" in mime_type:
        fmt = "GIF"

    redacted_image.save(output_buffer, format=fmt)
    output_buffer.seek(0)
    encoded = base64.b64encode(output_buffer.read()).decode("utf-8")

    return f"data:{mime_type};base64,{encoded}"


async def redact_openai_request(
    body: dict, http_client: httpx.AsyncClient
) -> Tuple[dict, Dict[str, str]]:
    combined_mapping = {}

    if "messages" not in body:
        return body, combined_mapping

    redacted_body = body.copy()
    redacted_messages = []

    for msg in body.get("messages", []):
        redacted_msg = msg.copy()
        content = msg.get("content")

        if isinstance(content, str) and content:
            redacted_content, mapping = redact_text_with_mapping(content)
            redacted_msg["content"] = redacted_content
            combined_mapping.update(mapping)
        elif isinstance(content, list):
            redacted_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    if text:
                        redacted_text, mapping = redact_text_with_mapping(text)
                        redacted_part = part.copy()
                        redacted_part["text"] = redacted_text
                        redacted_parts.append(redacted_part)
                        combined_mapping.update(mapping)
                    else:
                        redacted_parts.append(part)
                elif isinstance(part, dict) and part.get("type") == "image_url":
                    image_url_obj = part.get("image_url", {})
                    url = (
                        image_url_obj.get("url", "")
                        if isinstance(image_url_obj, dict)
                        else ""
                    )
                    if url:
                        try:
                            redacted_url = await redact_image(url, http_client)
                            redacted_part = part.copy()
                            redacted_part["image_url"] = {"url": redacted_url}
                            if (
                                isinstance(image_url_obj, dict)
                                and "detail" in image_url_obj
                            ):
                                redacted_part["image_url"]["detail"] = image_url_obj[
                                    "detail"
                                ]
                            redacted_parts.append(redacted_part)
                        except Exception:
                            redacted_parts.append(part)
                    else:
                        redacted_parts.append(part)
                else:
                    redacted_parts.append(part)
            redacted_msg["content"] = redacted_parts

        redacted_messages.append(redacted_msg)

    redacted_body["messages"] = redacted_messages
    return redacted_body, combined_mapping


def deanonymize_openai_response(body: dict, mapping: Dict[str, str]) -> dict:
    if not mapping:
        return body

    result = body.copy()

    if "choices" in result:
        redacted_choices = []
        for choice in result.get("choices", []):
            redacted_choice = choice.copy()
            message = choice.get("message", {})
            if message:
                redacted_message = message.copy()
                content = message.get("content")
                if isinstance(content, str):
                    redacted_message["content"] = deanonymize_text(content, mapping)
                redacted_choice["message"] = redacted_message

            delta = choice.get("delta", {})
            if delta:
                redacted_delta = delta.copy()
                content = delta.get("content")
                if isinstance(content, str):
                    redacted_delta["content"] = deanonymize_text(content, mapping)
                redacted_choice["delta"] = redacted_delta

            redacted_choices.append(redacted_choice)
        result["choices"] = redacted_choices

    return result


# --- Anthropic Messages API ---


async def redact_anthropic_request(
    body: dict, http_client: httpx.AsyncClient
) -> Tuple[dict, Dict[str, str]]:
    """Redact PII from Anthropic Messages API request."""
    combined_mapping = {}

    if "messages" not in body:
        return body, combined_mapping

    redacted_body = body.copy()
    redacted_messages = []

    for msg in body.get("messages", []):
        redacted_msg = msg.copy()
        content = msg.get("content")

        if isinstance(content, str) and content:
            redacted_content, mapping = redact_text_with_mapping(content)
            redacted_msg["content"] = redacted_content
            combined_mapping.update(mapping)
        elif isinstance(content, list):
            redacted_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    if text:
                        redacted_text, mapping = redact_text_with_mapping(text)
                        redacted_part = part.copy()
                        redacted_part["text"] = redacted_text
                        redacted_parts.append(redacted_part)
                        combined_mapping.update(mapping)
                    else:
                        redacted_parts.append(part)
                elif isinstance(part, dict) and part.get("type") == "image":
                    source = part.get("source", {})
                    if source.get("type") == "base64":
                        media_type = source.get("media_type", "image/png")
                        data = source.get("data", "")
                        if data:
                            try:
                                data_url = f"data:{media_type};base64,{data}"
                                redacted_url = await redact_image(data_url, http_client)
                                _, redacted_data = redacted_url.split(",", 1)
                                redacted_part = part.copy()
                                redacted_part["source"] = {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": redacted_data,
                                }
                                redacted_parts.append(redacted_part)
                            except Exception:
                                redacted_parts.append(part)
                        else:
                            redacted_parts.append(part)
                    else:
                        redacted_parts.append(part)
                else:
                    redacted_parts.append(part)
            redacted_msg["content"] = redacted_parts

        redacted_messages.append(redacted_msg)

    redacted_body["messages"] = redacted_messages

    # Also redact system prompt if present
    if "system" in body:
        system = body["system"]
        if isinstance(system, str) and system:
            redacted_system, mapping = redact_text_with_mapping(system)
            redacted_body["system"] = redacted_system
            combined_mapping.update(mapping)
        elif isinstance(system, list):
            redacted_system_parts = []
            for part in system:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    if text:
                        redacted_text, mapping = redact_text_with_mapping(text)
                        redacted_part = part.copy()
                        redacted_part["text"] = redacted_text
                        redacted_system_parts.append(redacted_part)
                        combined_mapping.update(mapping)
                    else:
                        redacted_system_parts.append(part)
                else:
                    redacted_system_parts.append(part)
            redacted_body["system"] = redacted_system_parts

    return redacted_body, combined_mapping


def deanonymize_anthropic_response(body: dict, mapping: Dict[str, str]) -> dict:
    """Restore PII in Anthropic Messages API response."""
    if not mapping:
        return body

    result = body.copy()

    if "content" in result:
        redacted_content = []
        for part in result.get("content", []):
            if isinstance(part, dict) and part.get("type") == "text":
                redacted_part = part.copy()
                text = part.get("text", "")
                if text:
                    redacted_part["text"] = deanonymize_text(text, mapping)
                redacted_content.append(redacted_part)
            else:
                redacted_content.append(part)
        result["content"] = redacted_content

    # Handle streaming delta
    if "delta" in result:
        delta = result["delta"]
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            text = delta.get("text", "")
            if text:
                result["delta"] = delta.copy()
                result["delta"]["text"] = deanonymize_text(text, mapping)

    return result


# --- OpenAI Responses API ---


async def redact_responses_api_request(
    body: dict, http_client: httpx.AsyncClient
) -> Tuple[dict, Dict[str, str]]:
    """Redact PII from OpenAI Responses API request."""
    combined_mapping = {}

    if "input" not in body:
        return body, combined_mapping

    redacted_body = body.copy()
    input_data = body["input"]

    # Simple string input
    if isinstance(input_data, str):
        redacted_input, mapping = redact_text_with_mapping(input_data)
        redacted_body["input"] = redacted_input
        combined_mapping.update(mapping)
        return redacted_body, combined_mapping

    # Array input (conversation format)
    if isinstance(input_data, list):
        redacted_input = []
        for item in input_data:
            if isinstance(item, dict):
                redacted_item = item.copy()
                content = item.get("content")

                if isinstance(content, str) and content:
                    redacted_content, mapping = redact_text_with_mapping(content)
                    redacted_item["content"] = redacted_content
                    combined_mapping.update(mapping)
                elif isinstance(content, list):
                    redacted_parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "input_text":
                            text = part.get("text", "")
                            if text:
                                redacted_text, mapping = redact_text_with_mapping(text)
                                redacted_part = part.copy()
                                redacted_part["text"] = redacted_text
                                redacted_parts.append(redacted_part)
                                combined_mapping.update(mapping)
                            else:
                                redacted_parts.append(part)
                        elif (
                            isinstance(part, dict) and part.get("type") == "input_image"
                        ):
                            image_url = part.get("image_url", "")
                            if image_url:
                                try:
                                    redacted_url = await redact_image(
                                        image_url, http_client
                                    )
                                    redacted_part = part.copy()
                                    redacted_part["image_url"] = redacted_url
                                    redacted_parts.append(redacted_part)
                                except Exception:
                                    redacted_parts.append(part)
                            else:
                                redacted_parts.append(part)
                        else:
                            redacted_parts.append(part)
                    redacted_item["content"] = redacted_parts

                redacted_input.append(redacted_item)
            else:
                redacted_input.append(item)

        redacted_body["input"] = redacted_input

    # Also handle instructions (system prompt)
    if "instructions" in body:
        instructions = body["instructions"]
        if isinstance(instructions, str) and instructions:
            redacted_instructions, mapping = redact_text_with_mapping(instructions)
            redacted_body["instructions"] = redacted_instructions
            combined_mapping.update(mapping)

    return redacted_body, combined_mapping


def deanonymize_responses_api_response(body: dict, mapping: Dict[str, str]) -> dict:
    """Restore PII in OpenAI Responses API response."""
    if not mapping:
        return body

    result = body.copy()

    if "output" in result:
        redacted_output = []
        for item in result.get("output", []):
            if isinstance(item, dict):
                redacted_item = item.copy()
                content = item.get("content")

                if isinstance(content, list):
                    redacted_content = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            redacted_part = part.copy()
                            text = part.get("text", "")
                            if text:
                                redacted_part["text"] = deanonymize_text(text, mapping)
                            redacted_content.append(redacted_part)
                        else:
                            redacted_content.append(part)
                    redacted_item["content"] = redacted_content

                redacted_output.append(redacted_item)
            else:
                redacted_output.append(item)

        result["output"] = redacted_output

    # Handle output_text directly in response
    if "output_text" in result:
        result["output_text"] = deanonymize_text(result["output_text"], mapping)

    return result


# --- Gemini API ---


async def redact_gemini_request(
    body: dict, http_client: httpx.AsyncClient
) -> Tuple[dict, Dict[str, str]]:
    """Redact PII from Gemini API request."""
    combined_mapping = {}

    if "contents" not in body:
        return body, combined_mapping

    redacted_body = body.copy()
    redacted_contents = []

    for content in body.get("contents", []):
        redacted_content = content.copy()
        parts = content.get("parts", [])

        if parts:
            redacted_parts = []
            for part in parts:
                if isinstance(part, dict):
                    if "text" in part:
                        text = part["text"]
                        if text:
                            redacted_text, mapping = redact_text_with_mapping(text)
                            redacted_part = part.copy()
                            redacted_part["text"] = redacted_text
                            redacted_parts.append(redacted_part)
                            combined_mapping.update(mapping)
                        else:
                            redacted_parts.append(part)
                    elif "inline_data" in part:
                        inline_data = part["inline_data"]
                        mime_type = inline_data.get("mime_type", "image/png")
                        data = inline_data.get("data", "")
                        if data and mime_type.startswith("image/"):
                            try:
                                data_url = f"data:{mime_type};base64,{data}"
                                redacted_url = await redact_image(data_url, http_client)
                                _, redacted_data = redacted_url.split(",", 1)
                                redacted_part = part.copy()
                                redacted_part["inline_data"] = {
                                    "mime_type": mime_type,
                                    "data": redacted_data,
                                }
                                redacted_parts.append(redacted_part)
                            except Exception:
                                redacted_parts.append(part)
                        else:
                            redacted_parts.append(part)
                    else:
                        redacted_parts.append(part)
                else:
                    redacted_parts.append(part)

            redacted_content["parts"] = redacted_parts

        redacted_contents.append(redacted_content)

    redacted_body["contents"] = redacted_contents

    # Handle system instruction
    if "system_instruction" in body:
        sys_instruction = body["system_instruction"]
        if isinstance(sys_instruction, dict):
            parts = sys_instruction.get("parts", [])
            if parts:
                redacted_parts = []
                for part in parts:
                    if isinstance(part, dict) and "text" in part:
                        text = part["text"]
                        if text:
                            redacted_text, mapping = redact_text_with_mapping(text)
                            redacted_part = part.copy()
                            redacted_part["text"] = redacted_text
                            redacted_parts.append(redacted_part)
                            combined_mapping.update(mapping)
                        else:
                            redacted_parts.append(part)
                    else:
                        redacted_parts.append(part)
                redacted_body["system_instruction"] = {"parts": redacted_parts}

    return redacted_body, combined_mapping


def deanonymize_gemini_response(body: dict, mapping: Dict[str, str]) -> dict:
    """Restore PII in Gemini API response."""
    if not mapping:
        return body

    result = body.copy()

    if "candidates" in result:
        redacted_candidates = []
        for candidate in result.get("candidates", []):
            redacted_candidate = candidate.copy()
            content = candidate.get("content", {})

            if content:
                redacted_content = content.copy()
                parts = content.get("parts", [])

                if parts:
                    redacted_parts = []
                    for part in parts:
                        if isinstance(part, dict) and "text" in part:
                            redacted_part = part.copy()
                            text = part["text"]
                            if text:
                                redacted_part["text"] = deanonymize_text(text, mapping)
                            redacted_parts.append(redacted_part)
                        else:
                            redacted_parts.append(part)

                    redacted_content["parts"] = redacted_parts

                redacted_candidate["content"] = redacted_content

            redacted_candidates.append(redacted_candidate)

        result["candidates"] = redacted_candidates

    return result


@app.api_route(
    "/api/openai/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    tags=["OpenAI Proxy"],
)
async def openai_proxy(request: Request, path: str):
    """
    Proxy to OpenAI API with automatic PII redaction.

    Supports both Chat Completions API (`/v1/chat/completions`) and
    Responses API (`/v1/responses`).

    **How it works:**
    1. Detects and redacts PII in your request (text and images)
    2. Forwards the sanitized request to OpenAI
    3. Restores original PII values in the response

    **Usage:**
    ```bash
    curl -X POST "https://anonymize.plenoai.com/api/openai/v1/chat/completions" \\
      -H "Authorization: Bearer $OPENAI_API_KEY" \\
      -H "Content-Type: application/json" \\
      -d '{"model": "gpt-5.2", "messages": [{"role": "user", "content": "Hello"}]}'
    ```
    """
    target_url = f"{OPENAI_API_BASE}/{path}"

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    body = await request.body()
    mapping = {}
    is_responses_api = False

    async with httpx.AsyncClient(timeout=120.0) as client:
        if request.method == "POST" and body:
            try:
                body_json = json.loads(body)
                # Chat Completions API (messages key)
                if "messages" in body_json:
                    redacted_body, mapping = await redact_openai_request(
                        body_json, client
                    )
                    body = json.dumps(redacted_body).encode("utf-8")
                # Responses API (input key)
                elif "input" in body_json:
                    is_responses_api = True
                    redacted_body, mapping = await redact_responses_api_request(
                        body_json, client
                    )
                    body = json.dumps(redacted_body).encode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        response = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
            params=request.query_params,
        )

    # Deanonymize response if we have a mapping
    response_content = response.content
    if mapping and response.status_code == 200:
        try:
            response_json = json.loads(response.content)
            if is_responses_api:
                deanonymized = deanonymize_responses_api_response(
                    response_json, mapping
                )
            else:
                deanonymized = deanonymize_openai_response(response_json, mapping)
            response_content = json.dumps(deanonymized).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    return Response(
        content=response_content,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.headers.get("content-type"),
    )


@app.api_route(
    "/api/anthropic/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    tags=["Anthropic Proxy"],
)
async def anthropic_proxy(request: Request, path: str):
    """
    Proxy to Anthropic Messages API with automatic PII redaction.

    **How it works:**
    1. Detects and redacts PII in messages and system prompts
    2. Forwards the sanitized request to Anthropic
    3. Restores original PII values in the response

    **Usage:**
    ```bash
    curl -X POST "https://anonymize.plenoai.com/api/anthropic/v1/messages" \\
      -H "x-api-key: $ANTHROPIC_API_KEY" \\
      -H "anthropic-version: 2023-06-01" \\
      -H "Content-Type: application/json" \\
      -d '{"model": "claude-opus-4-5", "max_tokens": 100, "messages": [...]}'
    ```
    """
    target_url = f"{ANTHROPIC_API_BASE}/{path}"

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    body = await request.body()
    mapping = {}

    async with httpx.AsyncClient(timeout=120.0) as client:
        if request.method == "POST" and body:
            try:
                body_json = json.loads(body)
                if "messages" in body_json:
                    redacted_body, mapping = await redact_anthropic_request(
                        body_json, client
                    )
                    body = json.dumps(redacted_body).encode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        response = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
            params=request.query_params,
        )

    response_content = response.content
    if mapping and response.status_code == 200:
        try:
            response_json = json.loads(response.content)
            deanonymized = deanonymize_anthropic_response(response_json, mapping)
            response_content = json.dumps(deanonymized).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    return Response(
        content=response_content,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.headers.get("content-type"),
    )


@app.api_route(
    "/api/gemini/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    tags=["Gemini Proxy"],
)
async def gemini_proxy(request: Request, path: str):
    """
    Proxy to Google Gemini API with automatic PII redaction.

    **How it works:**
    1. Detects and redacts PII in contents and system instructions
    2. Forwards the sanitized request to Gemini
    3. Restores original PII values in the response

    **Usage:**
    ```bash
    curl -X POST "https://anonymize.plenoai.com/api/gemini/v1beta/models/gemini-3-pro:generateContent?key=$GEMINI_API_KEY" \\
      -H "Content-Type: application/json" \\
      -d '{"contents": [{"parts": [{"text": "Hello"}]}]}'
    ```
    """
    target_url = f"{GEMINI_API_BASE}/{path}"

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    body = await request.body()
    mapping = {}

    async with httpx.AsyncClient(timeout=120.0) as client:
        if request.method == "POST" and body:
            try:
                body_json = json.loads(body)
                if "contents" in body_json:
                    redacted_body, mapping = await redact_gemini_request(
                        body_json, client
                    )
                    body = json.dumps(redacted_body).encode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        response = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
            params=request.query_params,
        )

    response_content = response.content
    if mapping and response.status_code == 200:
        try:
            response_json = json.loads(response.content)
            deanonymized = deanonymize_gemini_response(response_json, mapping)
            response_content = json.dumps(deanonymized).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    return Response(
        content=response_content,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.headers.get("content-type"),
    )
