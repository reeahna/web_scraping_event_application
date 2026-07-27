"""Phase 18: production-readiness checks, security headers, health endpoints."""

from __future__ import annotations

from types import SimpleNamespace

from app.core.production_checks import is_production_ready, production_blockers


def _settings(**over):
    base = dict(
        database_url="sqlite:///app.db", cookie_secure=False, behind_https=False,
        trusted_hosts=[], rate_limit_backend="memory",
        local_login_enabled=True, registration_enabled=True,
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
    assert is_production_ready(_settings()) is False


def test_hardened_settings_are_production_ready():
    hardened = _settings(
        database_url="postgresql://user:pw@db/app", cookie_secure=True, behind_https=True,
        trusted_hosts=["events.example.com"], rate_limit_backend="redis",
        registration_enabled=False,
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
