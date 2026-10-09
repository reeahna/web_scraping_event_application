import hashlib
import hmac
import secrets

# --- Session tokens --------------------------------------------------------------
# The raw token only ever lives in the client's cookie. The DB stores just its
# SHA-256 hash, so a database dump alone can't be replayed as a valid session.


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- CSRF (double-submit cookie) -------------------------------------------------


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_tokens_match(cookie_token: str | None, submitted_token: str | None) -> bool:
    if not cookie_token or not submitted_token:
        return False
    return hmac.compare_digest(cookie_token, submitted_token)
