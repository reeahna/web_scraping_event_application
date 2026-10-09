"""public_base_url falls back to the hosting platform's own address.

The live site spent weeks serving canonical links and a sitemap naming
http://localhost:8100 because PUBLIC_BASE_URL was never set in the dashboard.
Render sets RENDER_EXTERNAL_URL on every web service, so an unconfigured
deployment can use that instead of advertising an unreachable host.
"""

import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def _clean_platform_env(monkeypatch):
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_HOSTNAME", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)


def test_without_a_platform_the_development_default_stands():
    assert Settings().public_base_url == "http://localhost:8100"


def test_the_platform_url_replaces_the_local_default(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://city-events-ryjd.onrender.com")
    assert Settings().public_base_url == "https://city-events-ryjd.onrender.com"


def test_a_trailing_slash_is_trimmed(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://example.onrender.com/")
    assert Settings().public_base_url == "https://example.onrender.com"


def test_an_explicit_value_always_wins(monkeypatch):
    """A custom domain must not be overridden by the platform address."""
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://city-events-ryjd.onrender.com")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://bulletin.example")
    assert Settings().public_base_url == "https://bulletin.example"


def test_trusted_hosts_fall_back_too(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "city-events-ryjd.onrender.com")
    assert Settings().trusted_hosts == ["city-events-ryjd.onrender.com"]


def test_explicit_trusted_hosts_are_left_alone(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "city-events-ryjd.onrender.com")
    monkeypatch.setenv("TRUSTED_HOSTS", '["bulletin.example"]')
    assert Settings().trusted_hosts == ["bulletin.example"]


def test_the_fallback_clears_the_readiness_blocker(monkeypatch):
    """The point: a deployment that sets nothing is no longer misconfigured."""
    from app.core.production_checks import production_blockers

    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://city-events-ryjd.onrender.com")
    codes = {issue.code for issue in production_blockers(Settings())}
    assert "local_public_base_url" not in codes
