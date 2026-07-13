import hashlib
import hmac
import secrets

import bcrypt

# --- Password hashing (local dev/fallback login only) --------------------------

_BCRYPT_MAX_BYTES = 72  # bcrypt silently truncates beyond this; reject longer input.


def hash_password(password: str) -> str:
    if len(password.encode("utf-8")) > _BCRYPT_MAX_BYTES:
        raise ValueError("Password too long")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        return False


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
