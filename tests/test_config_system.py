"""Config, security baseline and Phase 4 stub behaviour."""

from __future__ import annotations

import pytest
from websockets.exceptions import InvalidStatus

from conftest import FakeModel, Turn
from roserver.config import Settings


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("ROSERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("ROSERVER_PORT", "9000")
    monkeypatch.setenv("ROSERVER_MAX_PENDING_INPUTS", "7")
    monkeypatch.setenv("ROSERVER_CORS_ORIGINS", "http://a.test, http://b.test")
    settings = Settings.from_env()
    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.max_pending_inputs == 7
    assert settings.cors_origins == ("http://a.test", "http://b.test")
    assert settings.resolved_ws_origins == ("http://a.test", "http://b.test")


def test_single_worker_contract(settings_factory):
    settings = settings_factory()
    assert settings.worker_warning(1) is None
    assert "one uvicorn worker" in (settings.worker_warning(4) or "")


def test_default_bind_is_loopback(settings_factory):
    assert settings_factory().host == "127.0.0.1"


def test_artifact_phase4_stub(client_factory, settings_factory):
    with client_factory(settings_factory(), model=FakeModel([Turn(text="x")])) as client:
        response = client.get("/api/v1/artifacts/sha256:" + "a" * 64)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "artifact_not_found"


def test_ws_origin_validation(client_factory, settings_factory):
    settings = settings_factory(cors_origins=("http://ok.test",))
    with client_factory(settings, model=FakeModel([Turn(text="x")])) as client:
        with client.websocket_connect(
            "/api/v1/runs/unknown/events", headers={"Origin": "http://ok.test"}
        ) as ws:
            assert ws.receive_json()["type"] == "stream.resync_required"

        with pytest.raises(InvalidStatus) as rejected:
            with client.websocket_connect(
                "/api/v1/runs/unknown/events", headers={"Origin": "http://evil.test"}
            ):
                pass
        # A disallowed Origin is refused during the handshake (HTTP 403).
        assert rejected.value.response.status_code == 403
