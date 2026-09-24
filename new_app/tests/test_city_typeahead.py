"""Suggestions as you type in the college-town chooser.

The town list travels with the page as a JSON data block so a suggestion
appears on the keystroke. The filtering itself lives in
app/static/js/city-search.js; what is verifiable here is that the page hands
that script correct data, and that the form still works without it.
"""

import json
import re

import pytest


@pytest.fixture
def towns(db_session, make_city):
    make_city(
        name="Bloomington", slug="bloomington-in",
        state_or_region="Indiana", university_name="Indiana University",
    )
    make_city(
        name="Bethlehem", slug="bethlehem-pa",
        state_or_region="Pennsylvania", university_name="Lehigh University",
    )
    make_city(name="Hidden", slug="hidden-town", is_active=False)
    db_session.commit()


def _index(html):
    block = re.search(
        r'<script type="application/json" id="city-index">(.*?)</script>', html, re.S
    )
    assert block, "no town index on the page"
    return json.loads(block.group(1))


def test_the_page_carries_the_town_index(client, towns):
    index = _index(client.get("/").text)
    names = {entry["name"] for entry in index}
    assert names == {"Bloomington", "Bethlehem"}


def test_the_index_carries_what_a_suggestion_shows(client, towns):
    entry = next(e for e in _index(client.get("/").text) if e["name"] == "Bloomington")
    assert entry["school"] == "Indiana University"
    assert entry["state"] == "Indiana"
    assert entry["slug"] == "bloomington-in"
    assert "count" in entry


def test_an_inactive_town_is_never_suggested(client, towns):
    """The index is built from the same search the visible grid uses."""
    assert "Hidden" not in {e["name"] for e in _index(client.get("/").text)}


def test_the_index_is_complete_even_when_the_page_is_filtered(client, towns):
    """Typing narrows the suggestions client-side, so a filtered page still
    has to carry every town or the box would stop suggesting after a search."""
    index = _index(client.get("/?q=lehigh").text)
    assert {e["name"] for e in index} == {"Bloomington", "Bethlehem"}


def test_the_input_is_wired_as_a_combobox(client, towns):
    html = client.get("/").text
    assert 'role="combobox"' in html
    assert 'aria-controls="city-suggestions"' in html
    assert 'role="listbox"' in html


def test_suggestions_start_closed(client, towns):
    """The ARIA state has to describe the no-script page: a reader must not be
    told about a listbox that is not open."""
    html = client.get("/").text
    assert 'aria-expanded="false"' in html
    assert re.search(r'id="city-suggestions"[^>]*\shidden', html)


def test_the_form_still_works_without_the_script(client, towns):
    """The suggestions are an enhancement; the GET form is the real path."""
    html = client.get("/").text
    assert '<form method="get" action="/"' in html
    results = client.get("/?q=ind").text
    grid = re.search(r'<ul class="city-grid">(.*?)</ul>', results, re.S).group(1)
    assert "Bloomington" in grid
    assert "Bethlehem" not in grid


def test_the_index_is_json_not_script(client, towns):
    """A data block, because the CSP forbids inline JavaScript."""
    html = client.get("/").text
    assert '<script type="application/json" id="city-index">' in html
    assert '<script src="/static/js/city-search.js"' in html


def test_town_names_cannot_inject_markup(client, db_session, make_city):
    """Names come from the database and are rendered by the script with
    textContent; the transport has to be safe too."""
    make_city(
        name='<img src=x onerror=alert(1)>', slug="xss-town",
        university_name='"></script><script>alert(2)</script>',
    )
    db_session.commit()
    html = client.get("/").text
    assert "<img src=x onerror" not in html
    assert "<script>alert(2)</script>" not in html
    # Still valid, parseable JSON carrying the real value.
    names = {entry["name"] for entry in _index(html)}
    assert '<img src=x onerror=alert(1)>' in names
