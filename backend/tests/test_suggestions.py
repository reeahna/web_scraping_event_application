"""Visitors suggest new towns and features; admins triage them."""

import re

from app.core.permissions import ADMINISTRATOR, EDITOR, REGISTERED_USER
from app.models.audit_log import AuditLog
from app.models.notification import Notification
from app.models.suggestion import Suggestion
from app.services import suggestions as svc


def _post(client, **fields):
    data = {"csrf_token": client.cookies.get("csrf_token"), **fields}
    return client.post("/suggest", data=data, follow_redirects=False)


def test_suggest_requires_sign_in(client):
    response = client.get(
        "/suggest", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login?next=%2Fsuggest")


def test_nav_link_sits_just_left_of_my_account(client, make_user, login):
    make_user(email="nav-s@example.com", role_name=REGISTERED_USER)
    login("nav-s@example.com")
    html = client.get("/account/saved").text
    nav = html[html.index('aria-label="Primary"'):html.index("</nav>")]
    links = re.findall(r'<a href="([^"]+)"', nav)
    assert links[links.index("/account") - 1] == "/suggest"


def test_admin_nav_has_suggest_and_suggestions(client, make_user, login):
    make_user(email="nav-a@example.com", role_name=ADMINISTRATOR)
    login("nav-a@example.com")
    html = client.get("/admin").text
    assert '<a href="/admin/suggestions"' in html
    end = html[html.index('class="admin-nav-end"'):]
    assert end.index('href="/suggest"') < end.index('href="/account"')


def test_town_suggestion_is_saved_and_shown_back(client, make_user, login, db_session):
    user = make_user(email="town@example.com", role_name=REGISTERED_USER)
    make_user(email="boss@example.com", role_name=ADMINISTRATOR)
    login("town@example.com")
    page = client.get("/suggest")
    assert page.status_code == 200
    assert 'name="university"' in page.text

    response = _post(
        client,
        kind="town",
        title="  Athens ",
        region="GA",
        university="University of Georgia",
        details="Calendar: https://calendar.uga.edu",
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/suggest?kind=town"

    row = db_session.query(Suggestion).one()
    assert (row.kind, row.title, row.region, row.university, row.status) == (
        "town", "Athens", "GA", "University of Georgia", "new"
    )
    assert row.user_id == user.id

    # Admins who manage reports hear about it.
    note = db_session.query(Notification).filter_by(notification_type="suggestion_submitted")
    assert note.count() == 1
    assert "Athens" in note.one().title

    mine = client.get("/suggest").text
    assert "Your suggestions" in mine
    assert "Athens, GA" in mine
    assert "University of Georgia" in mine


def test_town_needs_name_and_university(client, make_user, login, db_session):
    make_user(email="bad-town@example.com", role_name=REGISTERED_USER)
    login("bad-town@example.com")
    response = _post(client, kind="town", title="", university="")
    assert response.status_code == 422
    assert "Enter the town or city." in response.text
    assert "Enter the college or university there." in response.text
    assert db_session.query(Suggestion).count() == 0


def test_feature_suggestion_needs_title_and_details(client, make_user, login, db_session):
    make_user(email="feat@example.com", role_name=REGISTERED_USER)
    login("feat@example.com")
    assert _post(client, kind="feature", title="Dark mode").status_code == 422

    response = _post(
        client,
        kind="feature",
        title="Dark mode",
        details="Easier at night.",
        university="ignored",
    )
    assert response.status_code == 303
    row = db_session.query(Suggestion).one()
    assert (row.kind, row.title, row.details, row.university) == (
        "feature", "Dark mode", "Easier at night.", None
    )


def test_unknown_kind_falls_back_to_town(client, make_user, login, db_session):
    make_user(email="odd@example.com", role_name=REGISTERED_USER)
    login("odd@example.com")
    response = _post(client, kind="<script>", title="Ames", university="Iowa State")
    assert response.status_code == 303
    assert db_session.query(Suggestion).one().kind == "town"


def test_overlong_fields_are_rejected(client, make_user, login):
    make_user(email="long@example.com", role_name=REGISTERED_USER)
    login("long@example.com")
    response = _post(
        client, kind="feature", title="x" * (svc.TITLE_MAX + 1), details="ok"
    )
    assert response.status_code == 422
    assert f"under {svc.TITLE_MAX} characters" in response.text


def test_daily_limit(client, make_user, login, db_session, monkeypatch):
    monkeypatch.setattr(svc, "DAILY_LIMIT", 2)
    make_user(email="spam@example.com", role_name=REGISTERED_USER)
    login("spam@example.com")
    for i in range(2):
        assert _post(client, kind="feature", title=f"Idea {i}", details="d").status_code == 303
    response = _post(client, kind="feature", title="Idea 3", details="d")
    assert response.status_code == 429
    assert "try again tomorrow" in response.text
    assert db_session.query(Suggestion).count() == 2


def test_post_requires_csrf(client, make_user, login, db_session):
    make_user(email="csrf-s@example.com", role_name=REGISTERED_USER)
    login("csrf-s@example.com")
    response = client.post(
        "/suggest",
        data={"kind": "feature", "title": "t", "details": "d", "csrf_token": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code in (400, 403)
    assert db_session.query(Suggestion).count() == 0


def test_registered_users_cannot_see_admin_list(client, make_user, login):
    make_user(email="nosy@example.com", role_name=REGISTERED_USER)
    login("nosy@example.com")
    assert client.get("/admin/suggestions", follow_redirects=False).status_code == 403


def test_admin_lists_filters_and_updates(client, make_user, login, db_session):
    author = make_user(email="author@example.com", role_name=REGISTERED_USER)
    db_session.add_all(
        [
            Suggestion(user_id=author.id, kind="town", title="Ames", university="Iowa State"),
            Suggestion(user_id=author.id, kind="feature", title="Dark mode", details="pls"),
        ]
    )
    db_session.commit()
    make_user(email="triage@example.com", role_name=ADMINISTRATOR)
    login("triage@example.com")

    html = client.get("/admin/suggestions").text
    assert "Ames" in html and "Dark mode" in html
    towns = client.get("/admin/suggestions?kind=town").text
    assert "Ames" in towns and "Dark mode" not in towns

    ames = db_session.query(Suggestion).filter_by(title="Ames").one()
    response = client.post(
        f"/admin/suggestions/{ames.id}",
        data={
            "status": "planned",
            "admin_notes": "Found 9 calendars",
            "back": "/admin/suggestions?kind=town",
            "csrf_token": client.cookies.get("csrf_token"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/suggestions?kind=town"
    db_session.refresh(ames)
    assert (ames.status, ames.admin_notes) == ("planned", "Found 9 calendars")
    assert db_session.query(AuditLog).filter_by(action="suggestion_updated").count() == 1

    bad = client.post(
        f"/admin/suggestions/{ames.id}",
        data={"status": "hacked", "csrf_token": client.cookies.get("csrf_token")},
        follow_redirects=False,
    )
    assert bad.status_code == 422

    offsite = client.post(
        f"/admin/suggestions/{ames.id}",
        data={
            "status": "done",
            "back": "https://evil.example/",
            "csrf_token": client.cookies.get("csrf_token"),
        },
        follow_redirects=False,
    )
    assert offsite.headers["location"] == "/admin/suggestions"


def test_editor_can_view_but_not_change(client, make_user, login, db_session):
    db_session.add(Suggestion(kind="town", title="Ames", university="Iowa State"))
    db_session.commit()
    make_user(email="ed@example.com", role_name=EDITOR)
    login("ed@example.com")
    html = client.get("/admin/suggestions").text
    assert "Ames" in html
    assert "suggestion-status-form" not in html
    row = db_session.query(Suggestion).one()
    response = client.post(
        f"/admin/suggestions/{row.id}",
        data={"status": "done", "csrf_token": client.cookies.get("csrf_token")},
        follow_redirects=False,
    )
    assert response.status_code == 403
