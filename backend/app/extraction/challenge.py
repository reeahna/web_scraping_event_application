"""Recognising a bot-challenge / access-denied page from its *visible* text.

A challenge page says so in words a person reads ("Checking your browser",
"Access denied", "Performance & security by Cloudflare"). An ordinary page can
mention the same vendors only in its markup: a Cloudflare-hosted script, a
Turnstile or reCAPTCHA widget on a contact form, an analytics beacon. Matching
markers against raw HTML therefore marked ordinary sites as blocked, so every
check here runs on the page's visible text: scripts, styles, comments and tag
attributes are removed first.
"""

from __future__ import annotations

import re

_INVISIBLE_BLOCK_RE = re.compile(
    r"<(script|style|noscript|template|svg)\b[^>]*>.*?(</\1\s*>|$)", re.I | re.S
)
_COMMENT_RE = re.compile(r"<!--.*?(-->|$)", re.S)
_TAG_RE = re.compile(r"<[^>]*(>|$)", re.S)
_SPACE_RE = re.compile(r"\s+")
# "captcha" as a word of its own — a reCAPTCHA / hCaptcha brand notice on a
# form ("protected by reCAPTCHA") is not a challenge.
_CAPTCHA_RE = re.compile(r"(?<![a-z])captcha")


def visible_text(html: str) -> str:
    """Lower-cased visible text of an HTML fragment (which may be truncated
    mid-element)."""
    text = _COMMENT_RE.sub(" ", html)
    text = _INVISIBLE_BLOCK_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip().lower()


def find_marker(html: str, markers: tuple[str, ...]) -> str | None:
    """The first of `markers` present in `html`'s visible text, or None."""
    text = visible_text(html)
    for marker in markers:
        if marker == "captcha":
            if _CAPTCHA_RE.search(text):
                return marker
        elif marker in text:
            return marker
    return None
