"""The privacy policy and terms: reachable, linked, and in the sitemap."""

import pytest


@pytest.mark.parametrize(
    "path,heading", [("/privacy", "Privacy policy"), ("/terms", "Terms of use")]
)
def test_page_loads(client, path, heading):
    resp = client.get(path)
    assert resp.status_code == 200
    assert f"<h1>{heading}</h1>" in resp.text


def test_every_page_links_to_both_in_the_footer(client):
    page = client.get("/").text
    assert 'href="/privacy"' in page and 'href="/terms"' in page


def test_sign_up_points_at_both(client):
    page = client.get("/register").text
    assert 'href="/terms"' in page and 'href="/privacy"' in page


def test_both_are_in_the_sitemap(client):
    sitemap = client.get("/sitemap.xml").text
    assert "/privacy</loc>" in sitemap and "/terms</loc>" in sitemap


def test_contact_email_is_shown_when_configured(client, monkeypatch):
    from app.config import get_settings

    assert "mailto:" not in client.get("/privacy").text
    monkeypatch.setattr(get_settings(), "contact_email", "hello@example.com")
    assert 'href="mailto:hello@example.com"' in client.get("/privacy").text
