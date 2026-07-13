"""Email normalization policy.

Normalization rule (documented — see README "Account & registration"):
strip leading/trailing whitespace, then lowercase the entire address. This
means `User@Example.com` and `user@example.com` resolve to the same account.

We deliberately do NOT apply provider-specific tricks (Gmail dot-stripping,
plus-addressing removal, etc.) — those are provider policies, not a universal
email-address property, and guessing wrong would silently merge addresses
that are actually distinct mailboxes on non-Gmail providers.
"""


def normalize_email(email: str) -> str:
    return email.strip().lower()
