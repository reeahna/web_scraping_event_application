"""Phase 18: production-readiness checks, security headers, health endpoints."""

from __future__ import annotations

from types import SimpleNamespace

from app.core.production_checks import is_production_ready, production_blockers


def _settings(**over):
    base = dict(
        database_url="sqlite:///app.db", cookie_secure=False, behind_https=False,
        trusted_hosts=[], rate_limit_backend="memory",
        registration_enabled=True, superadmin_email=None,
        # The real development default, so this stands in for an unconfigured
        # deployment rather than an impossible one.
        public_base_url="http://localhost:8100",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_dev_defaults_are_not_production_ready():
    codes = {i.code for i in production_blockers(_settings())}
    assert "sqlite_database" in codes
    assert "insecure_cookies" in codes
    assert "no_https" in codes
    assert "no_trusted_hosts" in codes
    assert "in_process_rate_limit" in codes
    # The development default points at localhost, which would put an
    # unreachable host in every canonical link and sitemap entry.
    assert "local_public_base_url" in codes
    assert is_production_ready(_settings()) is False


def test_hardened_settings_are_production_ready():
    hardened = _settings(
        database_url="postgresql://user:pw@db/app", cookie_secure=True, behind_https=True,
        trusted_hosts=["events.example.com"], rate_limit_backend="redis",
        registration_enabled=False,
        public_base_url="https://events.example.com",
    )
    assert is_production_ready(hardened) is True


def test_security_headers_present(client):
    resp = client.get("/health")
    assert resp.headers.get("Content-Security-Policy")
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy")


def test_oversized_request_is_rejected(client):
    # A Content-Length beyond the configured max is refused before routing.
    big = "x" * 2_000_001
    resp = client.post("/health", content=big, headers={"content-type": "text/plain"})
    assert resp.status_code == 413


def test_liveness_and_readiness(client):
    assert client.get("/health/live").json()["status"] == "alive"
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    body = ready.json()
    assert body["checks"]["database"] == "ok"
    assert isinstance(body["checks"]["production_blockers"], list)


def test_readiness_reports_the_ai_label_queue(client, make_city, make_event, monkeypatch):
    from datetime import date, timedelta

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "groq_api_key", "test-key")
    city = make_city(name="Ready Queue City", slug="ready-queue-city")
    soon = date.today() + timedelta(days=2)
    make_event(city, title="Waiting", canonical_url="https://e.x/rq1", start_date=soon)

    queue = client.get("/health/ready").json()["checks"]["ai_categorization"]
    assert queue["enabled"] is True
    assert queue["provider"] == "groq"
    assert queue["waiting"] == 1
    assert 0 <= queue["oldest_waiting_hours"] < 1


def test_readiness_ai_queue_when_ai_is_off(client, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "groq_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    queue = client.get("/health/ready").json()["checks"]["ai_categorization"]
    assert queue == {"enabled": False, "provider": None}
