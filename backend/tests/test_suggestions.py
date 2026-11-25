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


# --- "New features / bug fixes" (/feedback) ---------------------------------------


def _post_feedback(client, **fields):
    data = {"csrf_token": client.cookies.get("csrf_token"), **fields}
    return client.post("/feedback", data=data, follow_redirects=False)


def test_feedback_is_open_without_sign_in(client):
    response = client.get("/feedback", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert "<h1>New features / bug fixes</h1>" in response.text
    # Visitors without an account may leave an address for a reply.
    assert 'name="contact_email"' in response.text
    assert 'name="website"' in response.text  # the honeypot


def test_footer_links_to_feedback(client, make_user, login):
    html = client.get("/").text
    assert 'href="/feedback">New features / bug fixes' in html
    make_user(email="foot@example.com", role_name=REGISTERED_USER)
    login("foot@example.com")
    html = client.get("/account/saved").text
    footer = html[html.index('class="site-footer"'):]
    assert 'href="/feedback">New features / bug fixes' in footer


def _anonymous_feedback(client, **fields):
    client.get("/feedback")  # sets the CSRF cookie
    return _post_feedback(client, **fields)


def test_anonymous_bug_report_is_saved_with_email(client, make_user, db_session):
    make_user(email="boss-anon@example.com", role_name=ADMINISTRATOR)
    response = _anonymous_feedback(
        client,
        kind="bug",
        title="Search is slow",
        details="Typing lags.",
        contact_email="  Visitor@Example.com ",
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/feedback?kind=bug"
    row = db_session.query(Suggestion).one()
    assert (row.kind, row.user_id, row.contact_email) == (
        "bug", None, "visitor@example.com"
    )
    note = (
        db_session.query(Notification)
        .filter_by(notification_type="suggestion_submitted")
        .one()
    )
    assert note.title == "New bug reported"
    # Without an account there is no "What you've sent" list to show.
    assert "Search is slow" not in client.get("/feedback").text


def test_anonymous_email_is_optional_but_checked(client, db_session):
    assert _anonymous_feedback(
        client, kind="feature", title="Dark mode", details="Night."
    ).status_code == 303
    assert db_session.query(Suggestion).one().contact_email is None
    bad = _post_feedback(
        client, kind="feature", title="x", details="y", contact_email="not an email"
    )
    assert bad.status_code == 422
    assert "name@example.com" in bad.text
    assert db_session.query(Suggestion).count() == 1


def test_signed_in_feedback_has_no_email_field(client, make_user, login, db_session):
    make_user(email="fb-signed@example.com", role_name=REGISTERED_USER)
    login("fb-signed@example.com")
    assert 'name="contact_email"' not in client.get("/feedback").text
    _post_feedback(
        client, kind="feature", title="t", details="d", contact_email="other@example.com"
    )
    assert db_session.query(Suggestion).one().contact_email is None


def test_honeypot_is_thanked_but_dropped(client, make_user, db_session):
    make_user(email="boss-hp@example.com", role_name=ADMINISTRATOR)
    response = _anonymous_feedback(
        client, kind="bug", title="Buy now", details="spam", website="http://spam.example"
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/feedback?kind=bug"
    assert db_session.query(Suggestion).count() == 0
    assert db_session.query(Notification).count() == 0


def test_anonymous_per_address_limit(client, db_session, monkeypatch):
    from app.dependencies import get_client_ip
    from app.main import app

    monkeypatch.setattr(svc, "ANONYMOUS_PER_IP_PER_HOUR", 2)
    address = {"ip": "203.0.113.9"}
    app.dependency_overrides[get_client_ip] = lambda: address["ip"]
    try:
        client.get("/feedback")
        token = client.cookies.get("csrf_token")

        def send(forged=None):
            headers = {"x-forwarded-for": forged} if forged else {}
            return client.post(
                "/feedback",
                data={"csrf_token": token, "kind": "bug", "title": "t", "details": "d"},
                headers=headers,
                follow_redirects=False,
            )

        assert send().status_code == 303
        assert send().status_code == 303
        blocked = send()
        assert blocked.status_code == 429
        assert "in the last hour" in blocked.text
        # A made-up X-Forwarded-For does not buy a fresh allowance: the limit
        # follows the trusted client address, not a header the sender writes.
        assert send(forged="198.51.100.77").status_code == 429
        # Another real address is not held back by the first one.
        address["ip"] = "198.51.100.4"
        assert send().status_code == 303
    finally:
        app.dependency_overrides.pop(get_client_ip, None)
    assert db_session.query(Suggestion).count() == 3


def test_anonymous_daily_ceiling(client, db_session, monkeypatch):
    monkeypatch.setattr(svc, "ANONYMOUS_DAILY_LIMIT", 1)
    assert _anonymous_feedback(client, kind="bug", title="a", details="b").status_code == 303
    second = _post_feedback(client, kind="bug", title="c", details="d")
    assert second.status_code == 429
    assert "try again tomorrow" in second.text
    assert db_session.query(Suggestion).count() == 1


def test_daily_ceiling_holds_under_concurrent_requests(db_session):
    """Many anonymous messages at once, each in its own session and connection
    (as on separate workers), never get past the ceiling. Runs on SQLite
    locally and on PostgreSQL in CI (TEST_DATABASE_URL)."""
    import threading

    from app.database import SessionLocal
    from app.models.suggestion import AnonymousFeedbackDay

    limit, senders = 5, 16
    original = svc.ANONYMOUS_DAILY_LIMIT
    svc.ANONYMOUS_DAILY_LIMIT = limit
    start = threading.Barrier(senders)
    outcomes: list[str] = []
    lock = threading.Lock()

    def send(n: int) -> None:
        session = SessionLocal()
        try:
            form = svc.parse_suggestion(
                kind="bug", title=f"race {n}", region="", university="", details="d"
            )
            start.wait()
            try:
                svc.create_suggestion(session, None, form)
                result = "saved"
            except svc.SuggestionLimitError:
                result = "limited"
            except Exception as exc:  # surfaced by the assertion below
                result = f"error: {exc!r}"
        finally:
            session.close()
        with lock:
            outcomes.append(result)

    try:
        threads = [threading.Thread(target=send, args=(n,)) for n in range(senders)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        svc.ANONYMOUS_DAILY_LIMIT = original

    assert sorted(set(outcomes)) == ["limited", "saved"], outcomes
    assert outcomes.count("saved") == limit
    assert outcomes.count("limited") == senders - limit
    db_session.expire_all()
    assert db_session.query(Suggestion).count() == limit
    assert db_session.query(AnonymousFeedbackDay).one().count == limit


def test_claim_is_given_back_when_the_message_is_not_saved(db_session):
    from app.models.suggestion import AnonymousFeedbackDay

    assert svc.claim_anonymous_slot(db_session, limit=1)
    db_session.rollback()  # the message failed to save
    assert db_session.query(AnonymousFeedbackDay).count() == 0
    assert svc.claim_anonymous_slot(db_session, limit=1)
    db_session.commit()
    assert not svc.claim_anonymous_slot(db_session, limit=1)
    db_session.rollback()
    assert db_session.query(AnonymousFeedbackDay).one().count == 1


def test_anonymous_post_requires_csrf(client, db_session):
    client.get("/feedback")
    response = client.post(
        "/feedback",
        data={"kind": "bug", "title": "t", "details": "d", "csrf_token": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code in (400, 403)
    assert db_session.query(Suggestion).count() == 0


def test_admin_list_shows_anonymous_sender(client, make_user, login, db_session):
    _anonymous_feedback(
        client, kind="bug", title="Anon bug", details="d", contact_email="anon@example.com"
    )
    make_user(email="boss-list@example.com", role_name=ADMINISTRATOR)
    login("boss-list@example.com")
    html = client.get("/admin/suggestions?kind=bug").text
    assert 'href="mailto:anon@example.com"' in html
    assert "Not signed in" in html


def test_feedback_page_has_feature_and_bug_forms(client, make_user, login):
    make_user(email="fb-page@example.com", role_name=REGISTERED_USER)
    login("fb-page@example.com")
    page = client.get("/feedback")
    assert page.status_code == 200
    assert "<h1>New features / bug fixes</h1>" in page.text
    assert "Send idea" in page.text
    bug = client.get("/feedback?kind=bug").text
    assert "Send bug report" in bug
    assert 'name="university"' not in bug
    # An unknown kind shows the feature form, never the town form.
    odd = client.get("/feedback?kind=town").text
    assert "Send idea" in odd and 'name="university"' not in odd


def test_bug_report_is_saved_notified_and_shown_back(client, make_user, login, db_session):
    user = make_user(email="bug@example.com", role_name=REGISTERED_USER)
    make_user(email="boss-b@example.com", role_name=ADMINISTRATOR)
    login("bug@example.com")
    assert _post_feedback(client, kind="bug", title="Map is blank").status_code == 422

    response = _post_feedback(
        client,
        kind="bug",
        title="  Map is   blank ",
        details="On /city/bloomington-in the map never loads.\nSafari on iPhone.",
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/feedback?kind=bug"
    row = db_session.query(Suggestion).one()
    assert (row.kind, row.title, row.user_id, row.status) == (
        "bug", "Map is blank", user.id, "new"
    )
    assert "\nSafari on iPhone." in row.details

    note = (
        db_session.query(Notification)
        .filter_by(notification_type="suggestion_submitted")
        .one()
    )
    assert note.title == "New bug reported"
    assert note.action_url == "/admin/suggestions?kind=bug"

    html = client.get("/feedback").text
    assert "What you&#39;ve sent" in html or "What you've sent" in html
    assert "Map is blank" in html

    row.status = "done"
    db_session.commit()
    assert "Fixed" in client.get("/feedback").text


def test_feature_idea_from_feedback_page(client, make_user, login, db_session):
    make_user(email="fb-feat@example.com", role_name=REGISTERED_USER)
    login("fb-feat@example.com")
    response = _post_feedback(
        client, kind="town", title="Dark mode", details="Easier at night."
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/feedback?kind=feature"
    assert db_session.query(Suggestion).one().kind == "feature"


def test_feedback_shares_the_daily_limit(client, make_user, login, db_session, monkeypatch):
    monkeypatch.setattr(svc, "DAILY_LIMIT", 2)
    make_user(email="fb-spam@example.com", role_name=REGISTERED_USER)
    login("fb-spam@example.com")
    assert _post(client, kind="town", title="Ames", university="ISU").status_code == 303
    assert _post_feedback(client, kind="bug", title="b", details="d").status_code == 303
    response = _post_feedback(client, kind="feature", title="f", details="d")
    assert response.status_code == 429
    assert "try again tomorrow" in response.text
    assert db_session.query(Suggestion).count() == 2


def test_feedback_post_requires_csrf(client, make_user, login, db_session):
    make_user(email="fb-csrf@example.com", role_name=REGISTERED_USER)
    login("fb-csrf@example.com")
    response = client.post(
        "/feedback",
        data={"kind": "bug", "title": "t", "details": "d", "csrf_token": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code in (400, 403)
    assert db_session.query(Suggestion).count() == 0


def test_old_feature_tab_moves_to_feedback(client, make_user, login):
    make_user(email="fb-old@example.com", role_name=REGISTERED_USER)
    login("fb-old@example.com")
    response = client.get("/suggest?kind=feature", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/feedback?kind=feature"
    assert 'href="/feedback?kind=feature"' in client.get("/suggest").text


def test_admin_filters_bug_reports(client, make_user, login, db_session):
    db_session.add_all(
        [
            Suggestion(kind="bug", title="Map is blank", details="d"),
            Suggestion(kind="feature", title="Dark mode", details="d"),
        ]
    )
    db_session.commit()
    make_user(email="fb-admin@example.com", role_name=ADMINISTRATOR)
    login("fb-admin@example.com")
    bugs = client.get("/admin/suggestions?kind=bug").text
    assert "Map is blank" in bugs and "Dark mode" not in bugs
    assert "<td>Bug</td>" in bugs
    assert ">Fixed</option>" in bugs
