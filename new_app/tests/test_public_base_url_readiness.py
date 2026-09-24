"""public_base_url has a development default that is wrong in production.

It shipped that way and the live site spent a while telling search engines
that every page's canonical URL was http://localhost:8100/, which is worse
than having no canonical at all: an unreachable canonical can get pages
dropped from the index. The readiness check now catches it, so the same
mistake cannot reach production silently again.
"""

import pytest

from app.config import get_settings
from app.core.production_checks import production_blockers
from app.services import seo


def _codes(**overrides):
    settings = get_settings().model_copy(update=overrides)
    return {issue.code for issue in production_blockers(settings)}


def test_the_development_default_is_a_production_blocker():
    assert "local_public_base_url" in _codes(public_base_url="http://localhost:8100")


@pytest.mark.parametrize(
    "value", ["", "http://127.0.0.1:8100", "http://localhost", "HTTP://LocalHost:8100"]
)
def test_every_local_form_is_caught(value):
    assert "local_public_base_url" in _codes(public_base_url=value)


def test_a_real_https_address_passes():
    codes = _codes(public_base_url="https://city-events-ryjd.onrender.com")
    assert "local_public_base_url" not in codes
    assert "insecure_public_base_url" not in codes


def test_a_plain_http_address_is_a_warning_not_a_blocker():
    codes = _codes(public_base_url="http://events.example.com")
    assert "local_public_base_url" not in codes
    assert "insecure_public_base_url" in codes


def test_absolute_urls_follow_the_setting(monkeypatch):
    """The whole point: every canonical link and sitemap entry comes from here."""
    settings = get_settings()
    monkeypatch.setattr(settings, "public_base_url", "https://example.org", raising=False)
    assert seo.absolute_url("/events/7") == "https://example.org/events/7"
    assert seo.absolute_url("city/bloomington-in") == "https://example.org/city/bloomington-in"


def test_a_trailing_slash_does_not_double_up(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "public_base_url", "https://example.org/", raising=False)
    assert seo.absolute_url("/events/7") == "https://example.org/events/7"
