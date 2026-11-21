"""Admin notifications inbox: Unread by default, Unread/Read/All and severity
filters, paging, counts, Mark read / Mark unread keeping the reader's place, and
no Dismiss button."""

import re
from datetime import UTC, datetime, timedelta

from app.core.permissions import REGISTERED_USER
from app.models.notification import Notification
from app.repositories.notification import count_unread_for_user, page_notifications_for_user

PASSWORD = "root-pass-1234"
BASE_TIME = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def _add(db, user, n, *, severity="info", read=False, start=0, same_time=False):
    """Add `n` notifications for `user`. Titles are "N<number>" and newer numbers
    are newer rows, so the expected newest-first order is easy to state."""
    rows = []
    for i in range(start, start + n):
        created = BASE_TIME if same_time else BASE_TIME + timedelta(minutes=i)
        rows.append(
            Notification(
                recipient_user_id=user.id,
                notification_type="test_event",
                severity=severity,
                title=f"N{i:04d}",
                message=f"message {i}",
                action_url=None,
                dedup_fingerprint=f"fp-{user.id}-{i}",
                created_at=created,
                read_at=created if read else None,
            )
        )
    db.add_all(rows)
    db.commit()
    return rows


def _titles(html: str) -> list[str]:
    return re.findall(r"<td>(N\d{4})</td>", html)


def _admin(make_super_admin, login, email):
    user = make_super_admin(email=email, password=PASSWORD)
    login(email, PASSWORD)
    return user


# --- repository ---------------------------------------------------------------


def test_paging_covers_every_row_once_even_with_identical_timestamps(db_session, make_user):
    user = make_user(email="inbox-repo@example.com")
    _add(db_session, user, 120, same_time=True)

    seen = []
    for page in (1, 2, 3):
        rows, total, current = page_notifications_for_user(
            db_session, user.id, page=page, per_page=50
        )
        assert total == 120
        assert current == page
        seen += [r.id for r in rows]
    assert len(seen) == 120
    assert len(set(seen)) == 120
    # Ties on created_at fall back to id, newest first.
    assert seen == sorted(seen, reverse=True)


def test_page_past_the_end_is_clamped_to_the_last_page(db_session, make_user):
    user = make_user(email="inbox-clamp@example.com")
    _add(db_session, user, 60)
    rows, total, current = page_notifications_for_user(db_session, user.id, page=9, per_page=50)
    assert (total, current, len(rows)) == (60, 2, 10)

    rows, total, current = page_notifications_for_user(
        db_session, user.id, read_state="read", page=3, per_page=50
    )
    assert (rows, total, current) == ([], 0, 1)


# --- list page ----------------------------------------------------------------


def test_more_than_fifty_pages_forward_and_back(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-pages@example.com")
    _add(db_session, admin, 125)

    first = client.get("/admin/notifications")
    assert first.status_code == 200
    titles = _titles(first.text)
    assert len(titles) == 50
    assert titles[0] == "N0124"
    assert "125 unread notifications" in first.text
    assert "Page 1 of 3" in first.text
    assert "125 unread" in first.text
    assert 'href="/admin/notifications?page=2"' in first.text

    third = client.get("/admin/notifications?page=3")
    assert _titles(third.text) == [f"N{i:04d}" for i in range(24, -1, -1)]
    assert "Page 3 of 3" in third.text
    assert 'href="/admin/notifications?page=2" rel="prev"' in third.text

    second = client.get("/admin/notifications?page=2")
    assert _titles(second.text)[0] == "N0074"
    assert _titles(second.text)[-1] == "N0025"

    every = _titles(first.text) + _titles(second.text) + _titles(third.text)
    assert len(every) == len(set(every)) == 125


def test_fifty_or_fewer_shows_no_pager(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-short@example.com")
    _add(db_session, admin, 3)
    page = client.get("/admin/notifications")
    assert "3 unread notifications" in page.text
    assert 'class="pagination"' not in page.text
    assert "Page 1 of" not in page.text


def test_read_and_unread_filters(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-read@example.com")
    _add(db_session, admin, 3, read=True, start=0)
    _add(db_session, admin, 2, read=False, start=3)

    unread = client.get("/admin/notifications?status=unread")
    assert _titles(unread.text) == ["N0004", "N0003"]
    assert "2 unread notifications" in unread.text

    read = client.get("/admin/notifications?status=read")
    assert _titles(read.text) == ["N0002", "N0001", "N0000"]
    # The unread count is inbox-wide, whatever is being shown.
    assert "2 unread" in read.text

    assert "3 read notifications" in read.text

    everything = client.get("/admin/notifications?status=all")
    assert len(_titles(everything.text)) == 5
    assert "5 notifications" in everything.text


def test_older_unread_only_link_still_works(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-legacy@example.com")
    _add(db_session, admin, 1, read=True, start=0)
    _add(db_session, admin, 1, read=False, start=1)
    page = client.get("/admin/notifications?unread_only=true")
    assert _titles(page.text) == ["N0001"]
    assert '<option value="unread" selected>' in page.text


def test_severity_and_combined_filters(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-sev@example.com")
    _add(db_session, admin, 2, severity="error", start=0)
    _add(db_session, admin, 1, severity="error", read=True, start=2)
    _add(db_session, admin, 2, severity="warning", start=3)
    _add(db_session, admin, 1, severity="info", start=5)

    assert _titles(client.get("/admin/notifications?status=all&severity=error").text) == [
        "N0002",
        "N0001",
        "N0000",
    ]
    assert _titles(client.get("/admin/notifications?severity=warning").text) == [
        "N0004",
        "N0003",
    ]
    assert _titles(client.get("/admin/notifications?severity=info").text) == ["N0005"]

    combined = client.get("/admin/notifications?severity=error")
    assert _titles(combined.text) == ["N0001", "N0000"]
    assert "2 unread notifications (errors)" in combined.text
    assert "5 unread" in combined.text

    read_errors = client.get("/admin/notifications?status=read&severity=error")
    assert _titles(read_errors.text) == ["N0002"]

    # Unknown values fall back to the defaults (Unread, any severity).
    assert len(_titles(client.get("/admin/notifications?severity=bogus").text)) == 5
    assert len(_titles(client.get("/admin/notifications?status=bogus").text)) == 5


def test_filters_survive_paging(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-keep@example.com")
    _add(db_session, admin, 60, severity="error", start=0)
    _add(db_session, admin, 10, severity="info", start=60)

    page = client.get("/admin/notifications?severity=error")
    assert "Page 1 of 2" in page.text
    assert 'href="/admin/notifications?severity=error&amp;page=2"' in page.text

    every = client.get("/admin/notifications?status=all")
    assert 'href="/admin/notifications?status=all&amp;page=2"' in every.text

    second = client.get("/admin/notifications?severity=error&page=2")
    assert _titles(second.text) == [f"N{i:04d}" for i in range(9, -1, -1)]


def test_empty_filter_result(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-empty@example.com")
    _add(db_session, admin, 2, severity="info")
    page = client.get("/admin/notifications?status=read&severity=error")
    assert page.status_code == 200
    assert "No notifications match these filters." in page.text
    assert "0 read notifications (errors)" in page.text
    assert "Back to unread" in page.text


def test_unread_view_says_when_everything_is_read(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-caught-up@example.com")
    _add(db_session, admin, 2, read=True)
    page = client.get("/admin/notifications")
    assert _titles(page.text) == []
    assert "All caught up" in page.text
    assert "No unread notifications. Choose Read or All to see earlier ones." in page.text


def test_unread_count_is_global_on_every_page(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-count@example.com")
    _add(db_session, admin, 70, read=False, start=0)
    _add(db_session, admin, 40, read=True, start=70)
    for url in (
        "/admin/notifications",
        "/admin/notifications?page=2",
        "/admin/notifications?page=3",
        "/admin/notifications?status=read",
        "/admin/notifications?severity=error",
    ):
        assert "70 unread" in client.get(url).text, url


# --- Mark read keeps your place ---------------------------------------------------


def test_mark_read_returns_to_same_page_and_filters(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-mark@example.com")
    rows = _add(db_session, admin, 120, severity="warning")
    client.get("/admin/notifications")
    target = rows[30]  # on page 2 of newest-first

    page = client.get("/admin/notifications?severity=warning&page=2")
    assert f'action="/admin/notifications/{target.id}/read"' in page.text
    assert '<input type="hidden" name="page" value="2" />' in page.text

    resp = client.post(
        f"/admin/notifications/{target.id}/read",
        data={"csrf_token": _csrf(client), "severity": "warning", "page": "2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/notifications?severity=warning&page=2"
    db_session.refresh(target)
    assert target.read_at is not None
    assert target.dismissed_at is None
    assert count_unread_for_user(db_session, admin.id) == 119


def test_mark_read_on_emptied_last_page_lands_on_nearest_page(
    client, make_super_admin, db_session, login
):
    admin = _admin(make_super_admin, login, "inbox-last@example.com")
    rows = _add(db_session, admin, 51)
    client.get("/admin/notifications")
    oldest = rows[0]  # the only row on page 2 of the unread view

    resp = client.post(
        f"/admin/notifications/{oldest.id}/read",
        data={"csrf_token": _csrf(client), "page": "2"},
        follow_redirects=False,
    )
    landed = client.get(resp.headers["location"])
    assert landed.status_code == 200
    assert len(_titles(landed.text)) == 50
    assert "50 unread" in landed.text
    assert resp.headers["location"] == "/admin/notifications?page=2"


def test_mark_read_redirect_ignores_unknown_values(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-safe@example.com")
    (row,) = _add(db_session, admin, 1)
    client.get("/admin/notifications")
    resp = client.post(
        f"/admin/notifications/{row.id}/read",
        data={"csrf_token": _csrf(client), "status": "https://evil.example", "severity": "x"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/admin/notifications"


def test_viewing_pages_marks_nothing_read(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-view@example.com")
    _add(db_session, admin, 60)
    for url in ("/admin/notifications", "/admin/notifications?page=2"):
        client.get(url)
    assert count_unread_for_user(db_session, admin.id) == 60


def test_mark_all_read_says_it_covers_the_whole_inbox(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-all-label@example.com")
    _add(db_session, admin, 2, severity="error", start=0)
    _add(db_session, admin, 3, severity="info", start=2)

    page = client.get("/admin/notifications")
    assert "Mark all inbox notifications as read" in page.text
    assert "not only the filtered list" not in page.text

    filtered = client.get("/admin/notifications?severity=error")
    assert "Mark all inbox notifications as read" in filtered.text
    assert "Applies to all 5 unread, not only the filtered list." in filtered.text

    client.post(
        "/admin/notifications/read-all",
        data={"csrf_token": _csrf(client), "severity": "error"},
        follow_redirects=False,
    )
    # The bulk action is inbox-wide, as the label says.
    assert count_unread_for_user(db_session, admin.id) == 0


def test_mark_all_read_keeps_filters(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-all@example.com")
    _add(db_session, admin, 2, severity="error")
    client.get("/admin/notifications")
    resp = client.post(
        "/admin/notifications/read-all",
        data={"csrf_token": _csrf(client), "status": "all", "severity": "error"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/admin/notifications?status=all&severity=error"


# --- permissions and CSRF ------------------------------------------------------


def test_mark_read_requires_csrf(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-csrf@example.com")
    (row,) = _add(db_session, admin, 1)
    client.get("/admin/notifications")
    resp = client.post(
        f"/admin/notifications/{row.id}/read",
        data={"csrf_token": "wrong", "page": "2"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    db_session.refresh(row)
    assert row.read_at is None


def test_filtered_and_paged_list_still_needs_admin(client, make_user, login):
    make_user(email="inbox-denied@example.com", password=PASSWORD, role_name=REGISTERED_USER)
    login("inbox-denied@example.com", PASSWORD)
    resp = client.get("/admin/notifications?status=unread&severity=error&page=2")
    assert resp.status_code == 403


def test_only_own_notifications_are_counted_and_listed(
    client, make_super_admin, make_user, db_session, login
):
    admin = _admin(make_super_admin, login, "inbox-own@example.com")
    other = make_user(email="inbox-other@example.com")
    _add(db_session, admin, 2, start=0)
    _add(db_session, other, 5, start=100)
    page = client.get("/admin/notifications")
    assert _titles(page.text) == ["N0001", "N0000"]
    assert "2 unread" in page.text


# --- Unread by default, Mark unread, no Dismiss ---------------------------------


def _nav_count(html: str) -> str:
    match = re.search(r">Notifications \((\d+)\)</a>", html)
    return match.group(1) if match else ""


def test_default_view_is_unread_and_mark_read_moves_it_out(
    client, make_super_admin, db_session, login
):
    admin = _admin(make_super_admin, login, "inbox-default@example.com")
    _add(db_session, admin, 1, read=True, start=0)
    rows = _add(db_session, admin, 2, start=1)
    target = rows[1]  # N0002
    created = target.created_at

    page = client.get("/admin/notifications")
    assert _titles(page.text) == ["N0002", "N0001"]
    assert '<option value="unread" selected>' in page.text
    assert '<option value="read" >' in page.text
    assert '<option value="all" >' in page.text
    assert _nav_count(page.text) == "2"

    resp = client.post(
        f"/admin/notifications/{target.id}/read",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/admin/notifications"
    after = client.get("/admin/notifications")
    assert _titles(after.text) == ["N0001"]
    assert _nav_count(after.text) == "1"
    assert "1 unread" in after.text

    # Still there, not deleted: in Read and in All, with its timestamps.
    assert _titles(client.get("/admin/notifications?status=read").text) == ["N0002", "N0000"]
    assert len(_titles(client.get("/admin/notifications?status=all").text)) == 3
    db_session.refresh(target)
    assert target.created_at.replace(tzinfo=None) == created.replace(tzinfo=None)
    assert target.read_at is not None


def test_mark_unread_puts_it_back_and_keeps_your_place(client, make_super_admin, db_session, login):
    from app.models.audit_log import AuditLog

    admin = _admin(make_super_admin, login, "inbox-unread@example.com")
    rows = _add(db_session, admin, 60, read=True, severity="warning")
    target = rows[5]  # on page 2 of the Read view
    created, title, url = target.created_at, target.title, target.action_url

    page = client.get("/admin/notifications?status=read&severity=warning&page=2")
    assert f'action="/admin/notifications/{target.id}/unread"' in page.text
    assert "Mark unread" in page.text
    assert "Mark read" not in page.text
    assert _nav_count(page.text) == "0"

    resp = client.post(
        f"/admin/notifications/{target.id}/unread",
        data={
            "csrf_token": _csrf(client),
            "status": "read",
            "severity": "warning",
            "page": "2",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == ("/admin/notifications?status=read&severity=warning&page=2")
    db_session.refresh(target)
    assert target.read_at is None
    assert (target.title, target.action_url) == (title, url)
    assert target.created_at.replace(tzinfo=None) == created.replace(tzinfo=None)
    assert count_unread_for_user(db_session, admin.id) == 1

    unread = client.get("/admin/notifications")
    assert _titles(unread.text) == [title]
    assert _nav_count(unread.text) == "1"

    audit = (
        db_session.query(AuditLog)
        .filter_by(action="notification_marked_unread", entity_id=target.id)
        .one()
    )
    assert audit.user_id == admin.id
    assert audit.entity_type == "notification"


def test_read_then_unread_both_audited(client, make_super_admin, db_session, login):
    from app.models.audit_log import AuditLog

    admin = _admin(make_super_admin, login, "inbox-audit@example.com")
    (row,) = _add(db_session, admin, 1)
    client.get("/admin/notifications")
    for action in ("read", "unread", "read"):
        client.post(
            f"/admin/notifications/{row.id}/{action}",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
    actions = [
        a.action
        for a in db_session.query(AuditLog)
        .filter_by(entity_type="notification", entity_id=row.id)
        .order_by(AuditLog.id)
    ]
    assert actions == [
        "notification_marked_read",
        "notification_marked_unread",
        "notification_marked_read",
    ]


def test_all_view_shows_the_right_action_per_row(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-allview@example.com")
    (read_row,) = _add(db_session, admin, 1, read=True, start=0)
    (unread_row,) = _add(db_session, admin, 1, start=1)
    page = client.get("/admin/notifications?status=all")
    assert f'action="/admin/notifications/{read_row.id}/unread"' in page.text
    assert f'action="/admin/notifications/{unread_row.id}/read"' in page.text
    assert '<input type="hidden" name="status" value="all" />' in page.text


def test_no_dismiss_button_and_dismiss_route_is_gone(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-nodismiss@example.com")
    (row,) = _add(db_session, admin, 1)
    for url in (
        "/admin/notifications",
        "/admin/notifications?status=read",
        "/admin/notifications?status=all",
    ):
        page = client.get(url)
        assert "Dismiss" not in page.text, url
        assert "/dismiss" not in page.text, url
    resp = client.post(
        f"/admin/notifications/{row.id}/dismiss",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code in (404, 405)
    db_session.refresh(row)
    assert row.dismissed_at is None


def test_previously_dismissed_notifications_keep_their_history(
    client, make_super_admin, db_session, login
):
    admin = _admin(make_super_admin, login, "inbox-olddismiss@example.com")
    (row,) = _add(db_session, admin, 1)
    dismissed = datetime(2026, 11, 10, tzinfo=UTC)
    row.dismissed_at = dismissed
    db_session.commit()

    page = client.get("/admin/notifications")
    assert _titles(page.text) == [row.title]
    assert _nav_count(page.text) == "1"
    client.post(
        f"/admin/notifications/{row.id}/read",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    db_session.refresh(row)
    assert row.read_at is not None
    assert row.dismissed_at.replace(tzinfo=None) == dismissed.replace(tzinfo=None)


def test_action_links_still_render(client, make_super_admin, db_session, login):
    admin = _admin(make_super_admin, login, "inbox-links@example.com")
    (row,) = _add(db_session, admin, 1)
    row.action_url = "/admin/websites/5"
    db_session.commit()
    page = client.get("/admin/notifications")
    assert '<a href="/admin/websites/5">view</a>' in page.text


def test_mark_unread_requires_csrf_admin_and_ownership(
    client, make_super_admin, make_user, db_session, login
):
    admin = _admin(make_super_admin, login, "inbox-unread-guard@example.com")
    other = make_user(email="inbox-unread-other@example.com")
    (mine,) = _add(db_session, admin, 1, read=True, start=0)
    (theirs,) = _add(db_session, other, 1, read=True, start=1)
    client.get("/admin/notifications")

    bad_csrf = client.post(
        f"/admin/notifications/{mine.id}/unread",
        data={"csrf_token": "wrong"},
        follow_redirects=False,
    )
    assert bad_csrf.status_code == 403
    db_session.refresh(mine)
    assert mine.read_at is not None

    not_mine = client.post(
        f"/admin/notifications/{theirs.id}/unread",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert not_mine.status_code == 404
    db_session.refresh(theirs)
    assert theirs.read_at is not None

    client.cookies.clear()
    make_user(email="inbox-unread-member@example.com", password=PASSWORD, role_name=REGISTERED_USER)
    login("inbox-unread-member@example.com", PASSWORD)
    client.get("/")
    member = client.post(
        f"/admin/notifications/{mine.id}/unread",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert member.status_code == 403
    db_session.refresh(mine)
    assert mine.read_at is not None
