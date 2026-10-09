"""People can delete their own account, and it leaves nothing personal behind."""

from datetime import UTC, datetime, timedelta

from app.core.permissions import REGISTERED_USER, SUPER_ADMINISTRATOR
from app.models.audit_log import AuditLog
from app.models.user import User
from app.models.user_engagement import SavedEvent, UserFollow
from app.models.user_session import UserSession
from app.services import engagement

TOMORROW = datetime.now(UTC).date() + timedelta(days=1)


def _signed_in(client, make_user, login, email="leaver@example.com", role=REGISTERED_USER):
    user = make_user(email=email, role_name=role)
    login(email)
    return user


def _delete(client, **form):
    return client.post(
        "/account/delete",
        data={"csrf_token": client.cookies.get("csrf_token"), **form},
        follow_redirects=False,
    )


def test_deleting_removes_the_account_and_everything_tied_to_it(
    client, make_user, login, db_session, make_city, make_website, make_event
):
    user = _signed_in(client, make_user, login)
    city = make_city()
    site = make_website(city, is_active=True, approved_pattern={"pattern_name": "static_html"})
    event = make_event(city, website=site, start_date=TOMORROW)
    engagement.save_event(db_session, user_id=user.id, event_id=event.id)
    engagement.follow(db_session, user_id=user.id, follow_type="city", target_id=city.id)
    user_id = user.id

    resp = _delete(client, confirmation="DELETE")
    assert resp.status_code == 303 and resp.headers["location"] == "/"

    db_session.expire_all()
    assert db_session.get(User, user_id) is None
    for model in (UserSession, SavedEvent, UserFollow):
        assert db_session.query(model).filter_by(user_id=user_id).count() == 0
    # Signed out, and the old session cannot be reused.
    assert client.get("/account", follow_redirects=False).status_code in (303, 401)


def test_the_email_address_is_scrubbed_from_the_audit_log(client, make_user, login, db_session):
    from app.services.audit import record_audit

    user = _signed_in(client, make_user, login)
    # Admin actions can name a person by address in the log's text.
    record_audit(db_session, actor_id=None, action="role_assigned", entity_type="user",
                 entity_id=user.id, detail="Editor given to leaver@example.com",
                 after={"email": "leaver@example.com"})
    user_id = user.id

    _delete(client, confirmation="DELETE")

    db_session.expire_all()
    for entry in db_session.query(AuditLog):
        for text in (entry.detail, entry.before_state, entry.after_state):
            assert "leaver@example.com" not in (text or "")
    deleted = db_session.query(AuditLog).filter_by(action="account_deleted").one()
    assert deleted.entity_id == user_id and deleted.user_id is None


def test_deleting_needs_delete_typed(client, make_user, login, db_session):
    user = _signed_in(client, make_user, login)
    resp = _delete(client, confirmation="yes")
    assert resp.status_code == 422
    assert "Type DELETE to confirm." in resp.text
    db_session.expire_all()
    assert db_session.get(User, user.id) is not None


def test_the_last_super_administrator_cannot_delete_themselves(
    client, make_user, login, db_session
):
    user = _signed_in(client, make_user, login, email="root@example.com", role=SUPER_ADMINISTRATOR)
    resp = _delete(client, confirmation="DELETE")
    assert resp.status_code == 403
    assert "only Super Administrator" in resp.text
    db_session.expire_all()
    assert db_session.get(User, user.id) is not None


def test_deleting_needs_a_csrf_token(client, make_user, login, db_session):
    user = _signed_in(client, make_user, login)
    resp = client.post("/account/delete", data={"csrf_token": "forged", "confirmation": "DELETE"})
    assert resp.status_code in (400, 403)
    db_session.expire_all()
    assert db_session.get(User, user.id) is not None


def test_the_account_page_offers_deletion(client, make_user, login):
    _signed_in(client, make_user, login)
    page = client.get("/account").text
    assert 'action="/account/delete"' in page
