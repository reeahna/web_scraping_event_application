from app.core.permissions import EDITOR, REGISTERED_USER
from app.models.audit_log import AuditLog


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def _structured_form(**overrides) -> dict:
    form = {
        "pattern_name": "json_ld_event",
        "listing_url": "https://example.com/events",
        "api_endpoint": "",
        "timezone": "",
        "event_container_selector": "",
        "detail_page_selector": "",
        "max_detail_fetches": "25",
        "pagination_strategy": "none",
        "page_param": "",
        "page_size_param": "",
        "next_page_selector": "",
        "max_pages": "10",
        "max_events": "500",
        "date_formats": "",
        "time_formats": "",
        "field_selectors": "",
        "json_paths": "",
        "transformations": "",
        "category_mappings": "",
        "exclusion_rules": "",
        "geographic_filters": "",
        "raw_json": "",
    }
    form.update(overrides)
    return form


def test_configure_page_prompts_when_no_draft_exists(
    client, make_super_admin, make_city, make_website, login
):
    make_super_admin(email="configure-root@example.com", password="root-pass-1234")
    city = make_city(name="Configure City", slug="configure-city")
    website = make_website(city, name="Configure Site")
    login("configure-root@example.com", "root-pass-1234")

    resp = client.get(f"/admin/websites/{website.id}/configure")
    assert resp.status_code == 200
    assert "detection" in resp.text.lower() or "select a pattern" in resp.text.lower()


def test_structured_configuration_form_saves_draft(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="configure-root2@example.com", password="root-pass-1234")
    city = make_city(name="Configure City 2", slug="configure-city-2")
    website = make_website(city, name="Configure Site 2")
    login("configure-root2@example.com", "root-pass-1234")

    data = _structured_form(
        field_selectors='{"title": {"kind": "css", "selector": ".title"}}',
        required_fields=["title", "start_date", "canonical_url"],
    )
    data["csrf_token"] = _csrf(client)
    resp = client.post(f"/admin/websites/{website.id}/configure", data=data, follow_redirects=False)
    assert resp.status_code == 303
    db_session.refresh(website)
    assert website.configuration["pattern_name"] == "json_ld_event"
    assert website.configuration["field_selectors"]["title"]["selector"] == ".title"
    assert website.configuration_version == 1

    entries = (
        db_session.query(AuditLog).filter(AuditLog.action == "website_configuration_updated").all()
    )
    assert len(entries) == 1


def test_structured_configuration_form_rejects_invalid_scoped_json(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="configure-root3@example.com", password="root-pass-1234")
    city = make_city(name="Configure City 3", slug="configure-city-3")
    website = make_website(city, name="Configure Site 3")
    login("configure-root3@example.com", "root-pass-1234")

    data = _structured_form(field_selectors="{not valid json")
    data["csrf_token"] = _csrf(client)
    resp = client.post(f"/admin/websites/{website.id}/configure", data=data, follow_redirects=False)
    assert resp.status_code == 422
    db_session.refresh(website)
    assert website.configuration is None


def test_advanced_raw_json_editor_rejects_unknown_fields(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="configure-root4@example.com", password="root-pass-1234")
    city = make_city(name="Configure City 4", slug="configure-city-4")
    website = make_website(city, name="Configure Site 4")
    login("configure-root4@example.com", "root-pass-1234")

    data = _structured_form(
        raw_json='{"pattern_name": "json_ld_event", "listing_url": "https://example.com/events", '
        '"totally_unknown_field": true}'
    )
    data["csrf_token"] = _csrf(client)
    resp = client.post(f"/admin/websites/{website.id}/configure", data=data, follow_redirects=False)
    assert resp.status_code == 422
    db_session.refresh(website)
    assert website.configuration is None


def test_advanced_raw_json_editor_accepts_valid_full_configuration(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="configure-root5@example.com", password="root-pass-1234")
    city = make_city(name="Configure City 5", slug="configure-city-5")
    website = make_website(city, name="Configure Site 5")
    login("configure-root5@example.com", "root-pass-1234")

    data = _structured_form(
        raw_json='{"pattern_name": "json_ld_event", "listing_url": "https://example.com/events"}'
    )
    data["csrf_token"] = _csrf(client)
    resp = client.post(f"/admin/websites/{website.id}/configure", data=data, follow_redirects=False)
    assert resp.status_code == 303
    db_session.refresh(website)
    assert website.configuration["pattern_name"] == "json_ld_event"


def test_configure_route_requires_sites_update_permission(
    client, make_user, make_city, make_website, login
):
    city = make_city(name="Configure Denied City", slug="configure-denied-city")
    website = make_website(city, name="Configure Denied Site")
    make_user(
        email="configure-denied@example.com",
        password="denied-pass-123",
        role_name=REGISTERED_USER,
    )
    login("configure-denied@example.com", "denied-pass-123")

    assert client.get(f"/admin/websites/{website.id}/configure").status_code == 403


def test_editor_can_configure(client, make_user, make_city, make_website, login, db_session):
    city = make_city(name="Configure Editor City", slug="configure-editor-city")
    website = make_website(city, name="Configure Editor Site")
    make_user(email="configure-editor@example.com", password="editor-pass-123", role_name=EDITOR)
    login("configure-editor@example.com", "editor-pass-123")

    data = _structured_form()
    data["csrf_token"] = _csrf(client)
    resp = client.post(f"/admin/websites/{website.id}/configure", data=data, follow_redirects=False)
    assert resp.status_code == 303
    db_session.refresh(website)
    assert website.configuration is not None


def test_configure_form_does_not_expose_generic_edit_form_for_configuration(
    client, make_super_admin, make_city, make_website, login, db_session
):
    """The generic website edit form must no longer offer configuration/
    proposed_pattern/approved_pattern as mass-assignable fields."""
    make_super_admin(email="configure-root6@example.com", password="root-pass-1234")
    city = make_city(name="Configure City 6", slug="configure-city-6")
    website = make_website(city, name="Configure Site 6")
    login("configure-root6@example.com", "root-pass-1234")

    resp = client.post(
        f"/admin/websites/{website.id}",
        data={
            "name": website.name,
            "base_url": website.base_url,
            "city_id": str(city.id),
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(website)
    assert website.approved_pattern is None


_ICS_CONFIG = {
    "pattern_name": "ics_calendar",
    "api_endpoint": "https://example.com/calendar.ics",
    "fetch": {"allowed_content_types": ["text/calendar", "text/plain"]},
    "required_fields": ["title", "start_date"],
    "max_detail_fetches": 0,
}


def _ics_website(make_super_admin, make_city, make_website, login, db_session, slug):
    make_super_admin(email=f"{slug}@example.com")
    city = make_city(name=f"City {slug}", slug=slug)
    website = make_website(city, name=f"Site {slug}")
    website.configuration = dict(_ICS_CONFIG)
    website.configuration_version = 1
    db_session.commit()
    login(f"{slug}@example.com")
    return website


def _as_form(fields: list[tuple[str, str]]) -> dict[str, list[str]]:
    form: dict[str, list[str]] = {}
    for name, value in fields:
        form.setdefault(name, []).append(value)
    return form


def _form_as_shown(client, website_id: int) -> list[tuple[str, str]]:
    """Every field the configure page renders, with its value, the way a
    browser would submit it unchanged."""
    import html
    import re

    page = client.get(f"/admin/websites/{website_id}/configure").text
    form = page[page.index('action="/admin/websites/'):]
    fields: list[tuple[str, str]] = []
    for attrs in re.findall(r"<input([^>]*)>", form):
        name = re.search(r'name="([^"]+)"', attrs)
        if not name:
            continue
        value = re.search(r'value="([^"]*)"', attrs)
        value = html.unescape(value.group(1)) if value else ""
        if 'type="checkbox"' in attrs:
            if re.search(r"\schecked", attrs):
                fields.append((name.group(1), value or "on"))
            continue
        fields.append((name.group(1), value))
    textarea = r'<textarea[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>'
    for name, value in re.findall(textarea, form, re.S):
        fields.append((name, html.unescape(value)))
    for name, options in re.findall(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', form, re.S):
        chosen = re.search(r'value="([^"]*)"\s*selected', options)
        fields.append((name, chosen.group(1) if chosen else ""))
    return fields


def test_saving_the_form_unchanged_keeps_feed_settings(
    client, make_super_admin, make_city, make_website, login, db_session
):
    """The page shows no fetch settings, and used to hide the API endpoint for
    an iCalendar feed, so saving it reset both and every preview failed."""
    website = _ics_website(make_super_admin, make_city, make_website, login, db_session, "ics1")
    data = _form_as_shown(client, website.id)
    assert ("api_endpoint", "https://example.com/calendar.ics") in data
    resp = client.post(
        f"/admin/websites/{website.id}/configure", data=_as_form(data), follow_redirects=False
    )
    assert resp.status_code == 303
    db_session.refresh(website)
    assert website.configuration["api_endpoint"] == "https://example.com/calendar.ics"
    assert "text/calendar" in website.configuration["fetch"]["allowed_content_types"]


def test_editing_a_shown_field_keeps_the_rest(
    client, make_super_admin, make_city, make_website, login, db_session
):
    website = _ics_website(make_super_admin, make_city, make_website, login, db_session, "ics2")
    rule = '[{"field": "title", "kind": "regex_extract_group", '
    rule += '"params": {"pattern": "^(?:\\\\[[A-Z]\\\\]\\\\s*)?(.+)$", "group": 1}}]'
    data = [(k, v) for k, v in _form_as_shown(client, website.id) if k != "transformations"]
    data.append(("transformations", rule))
    resp = client.post(
        f"/admin/websites/{website.id}/configure", data=_as_form(data), follow_redirects=False
    )
    assert resp.status_code == 303
    db_session.refresh(website)
    assert website.configuration["transformations"][0]["kind"] == "regex_extract_group"
    assert "text/calendar" in website.configuration["fetch"]["allowed_content_types"]


def test_an_unsafe_transformation_regex_is_rejected_on_save(
    client, make_super_admin, make_city, make_website, login, db_session
):
    website = _ics_website(make_super_admin, make_city, make_website, login, db_session, "ics3")
    rule = '[{"field": "title", "kind": "regex_extract_group", '
    rule += '"params": {"pattern": "(a+)+$", "group": 1}}]'
    data = [(k, v) for k, v in _form_as_shown(client, website.id) if k != "transformations"]
    data.append(("transformations", rule))
    resp = client.post(
        f"/admin/websites/{website.id}/configure", data=_as_form(data), follow_redirects=False
    )
    assert resp.status_code == 422
    assert "high-risk" in resp.text
    db_session.refresh(website)
    assert website.configuration_version == 1
