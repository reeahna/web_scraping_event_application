import pytest

from app.models.event import Event
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.website_configuration import approve_configuration
from tests.extraction_helpers import html_handler, patched_http_fetch


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


@pytest.mark.asyncio
async def test_event_detail_shows_real_provenance_for_permitted_role(
    client, make_super_admin, make_city, make_website, login, db_session
):
    admin = make_super_admin(email="prov-root@example.com", password="root-pass-1234")
    city = make_city(name="Provenance City", slug="provenance-city")
    website = make_website(city, name="Provenance Site")
    website.configuration = SiteConfiguration(
        pattern_name="json_ld_event", listing_url="https://example.com/events"
    ).model_dump(mode="json")
    db_session.commit()

    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)
    approve_configuration(db_session, website, approved_by_user_id=admin.id)

    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await run_extraction(db_session, website, triggered_by_user_id=admin.id)

    event = db_session.query(Event).one()
    login("prov-root@example.com", "root-pass-1234")

    response = client.get(f"/admin/events/{event.id}")
    assert response.status_code == 200
    body = response.text
    assert "json_ld_event" in body
    assert "future extraction data" not in body  # old placeholder text is gone
    assert "example.com/events" in body


def test_event_detail_shows_no_provenance_message_for_manually_present_event(
    client, make_super_admin, make_city, make_event, login, db_session
):
    make_super_admin(email="prov-root2@example.com", password="root-pass-1234")
    city = make_city(name="No Provenance City", slug="no-provenance-city")
    event = make_event(city, title="Manually Tracked Event")
    login("prov-root2@example.com", "root-pass-1234")

    response = client.get(f"/admin/events/{event.id}")
    assert response.status_code == 200
    assert "No extraction provenance recorded" in response.text
