"""Failed logins are limited per account and per address."""

from app.services import rate_limit

PASSWORD = "correct-horse-battery"


def _fail(login, email="victim@example.com", times=1):
    for _ in range(times):
        resp = login(email, "wrong-password")
    return resp


def test_an_account_locks_after_too_many_failures_even_with_the_right_password(
    login, make_user
):
    make_user(email="victim@example.com", password=PASSWORD)
    assert _fail(login, times=rate_limit.LOGIN_FAILURES_PER_ACCOUNT).status_code == 401

    resp = login("victim@example.com", PASSWORD)
    assert resp.status_code == 429
    assert "Too many failed attempts" in resp.text
    assert "session_token" not in resp.cookies


def test_the_lock_is_per_account(login, make_user):
    make_user(email="victim@example.com", password=PASSWORD)
    make_user(email="bystander@example.com", password=PASSWORD)
    _fail(login, times=rate_limit.LOGIN_FAILURES_PER_ACCOUNT)
    assert login("bystander@example.com", PASSWORD).status_code == 303


def test_email_case_and_spacing_do_not_dodge_the_limit(login, make_user):
    make_user(email="victim@example.com", password=PASSWORD)
    for variant in ["VICTIM@example.com", " victim@example.com", "Victim@Example.com"]:
        _fail(login, email=variant, times=rate_limit.LOGIN_FAILURES_PER_ACCOUNT // 3)
    _fail(login, times=rate_limit.LOGIN_FAILURES_PER_ACCOUNT % 3 or 1)
    assert login("victim@example.com", PASSWORD).status_code == 429


def test_one_address_guessing_across_many_accounts_is_stopped(login, make_user):
    make_user(email="target@example.com", password=PASSWORD)
    for i in range(rate_limit.LOGIN_FAILURES_PER_IP):
        login(f"guess{i}@example.com", "nope")
    assert login("target@example.com", PASSWORD).status_code == 429


def test_a_successful_login_clears_the_account_count(login, make_user):
    make_user(email="victim@example.com", password=PASSWORD)
    _fail(login, times=rate_limit.LOGIN_FAILURES_PER_ACCOUNT - 1)
    assert login("victim@example.com", PASSWORD).status_code == 303
    _fail(login, times=rate_limit.LOGIN_FAILURES_PER_ACCOUNT - 1)
    assert login("victim@example.com", PASSWORD).status_code == 303


def test_the_lock_expires(login, make_user, monkeypatch):
    make_user(email="victim@example.com", password=PASSWORD)
    _fail(login, times=rate_limit.LOGIN_FAILURES_PER_ACCOUNT)
    assert login("victim@example.com", PASSWORD).status_code == 429

    real = rate_limit.time.monotonic
    monkeypatch.setattr(
        rate_limit.time, "monotonic", lambda: real() + rate_limit.LOGIN_WINDOW_SECONDS + 1
    )
    assert login("victim@example.com", PASSWORD).status_code == 303


def test_redis_backend_counts_and_resets():
    import fakeredis

    backend = rate_limit.RedisRateLimitBackend(fakeredis.FakeRedis())
    for _ in range(3):
        backend.allow("k", limit=10, window_seconds=60)
    assert backend.count("k", window_seconds=60) == 3
    backend.reset("k")
    assert backend.count("k", window_seconds=60) == 0
