"""External authentication providers (Phase 14).

A provider-independent OAuth/OIDC layer built on Authlib (we never hand-roll
the protocol). Google, Microsoft, and Facebook are each configured
independently and are only offered when they have credentials, so the app runs
with any or all disabled. Tests inject a MockProvider — no real OAuth app is
ever contacted.
"""

from app.services.oauth.providers import (
    ExternalIdentityInfo,
    MockProvider,
    OAuthProvider,
    ProviderError,
    build_provider,
    enabled_provider_names,
    is_provider_enabled,
)

__all__ = [
    "ExternalIdentityInfo",
    "MockProvider",
    "OAuthProvider",
    "ProviderError",
    "build_provider",
    "enabled_provider_names",
    "is_provider_enabled",
]
