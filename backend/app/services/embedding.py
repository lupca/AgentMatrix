"""Text embedding service backed by the configured embedding API."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from app.db.models import Setting


class EmbeddingError(RuntimeError):
    """Raised when text cannot be embedded."""


def _setting_value(db: Session, key: str) -> str:
    setting = db.get(Setting, key)
    value = setting.value if setting is not None else None
    if value is None or not str(value).strip():
        raise EmbeddingError(f"Missing required embedding setting: {key}")
    return str(value).strip()


def embed_text(text: str, db: Session) -> list[float]:
    """Embed text using the URL, key, and model configured in the settings table."""

    if not isinstance(text, str) or not text.strip():
        raise EmbeddingError("Text to embed must be a non-empty string")

    api_url = _setting_value(db, "embedding_api_url").rstrip("/") + "/embeddings"
    api_key = _setting_value(db, "embedding_api_key")
    model = _setting_value(db, "embedding_model")
    request_body = json.dumps({"model": model, "input": text}).encode("utf-8")
    request = Request(
        api_url,
        data=request_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise EmbeddingError(
            f"Embedding API request failed with HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except URLError as exc:
        raise EmbeddingError(f"Embedding API request failed: {exc.reason}") from exc
    except OSError as exc:
        raise EmbeddingError(f"Embedding API request failed: {exc}") from exc

    try:
        body: Any = json.loads(response_body)
        embedding = body["data"][0]["embedding"]
        if not isinstance(embedding, list) or not embedding:
            raise ValueError("embedding is not a non-empty list")
        return [float(value) for value in embedding]
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EmbeddingError(f"Embedding API returned an invalid response: {exc}") from exc
