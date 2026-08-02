import json

import pytest

from app.services.embedding import EmbeddingError, embed_text
from app.services.entity_admin import update_setting


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def test_embed_text_posts_configured_request(db_session, monkeypatch):
    update_setting(db_session, "embedding_api_url", "https://embedding.example/v1/")
    update_setting(db_session, "embedding_api_key", "secret-key")
    update_setting(db_session, "embedding_model", "test-model")
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse({"data": [{"embedding": [1, 2.5, 3]}]})

    monkeypatch.setattr("app.services.embedding.urlopen", fake_urlopen)

    assert embed_text("hello", db_session) == [1.0, 2.5, 3.0]
    request, timeout = requests[0]
    assert request.full_url == "https://embedding.example/v1/embeddings"
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer secret-key"
    assert json.loads(request.data) == {"model": "test-model", "input": "hello"}
    assert timeout == 30


def test_embed_text_reports_missing_configuration(db_session):
    with pytest.raises(EmbeddingError, match="embedding_api_url"):
        embed_text("hello", db_session)


def test_embed_text_reports_invalid_response(db_session, monkeypatch):
    update_setting(db_session, "embedding_api_url", "https://embedding.example/v1")
    update_setting(db_session, "embedding_api_key", "secret-key")
    update_setting(db_session, "embedding_model", "test-model")
    monkeypatch.setattr(
        "app.services.embedding.urlopen",
        lambda request, timeout: FakeResponse({"data": []}),
    )

    with pytest.raises(EmbeddingError, match="invalid response"):
        embed_text("hello", db_session)
