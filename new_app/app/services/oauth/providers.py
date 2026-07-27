"""OAuth/OIDC provider adapters.

`AuthlibProvider` uses Authlib's OAuth2 client for the real authorization-URL
build, code->token exchange, and userinfo fetch — the protocol is never
implemented by hand here. Each provider is described by a `ProviderSpec`
(endpoints + scopes + how to read its userinfo). `MockProvider` returns a
canned identity for tests without any network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class ProviderError(Exception):
    """Provider is unknown/disabled, or a token/userinfo exchange failed."""


@dataclass(frozen=True)
class ExternalIdentityInfo:
    provider: str
    subject: str
    email: str | None
    email_verified: bool
    display_name: str | None
    avatar_url: str | None


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    scopes: str
    is_oidc: bool  # OIDC providers use a nonce and return a `sub`
    subject_field: str
    email_field: str
    verified_field: str | None
    name_field: str
    avatar_field: str | None


_SPECS: dict[str, ProviderSpec] = {
    "google": ProviderSpec(
        name="google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
        scopes="openid email profile",
        is_oidc=True,
        subject_field="sub", email_field="email", verified_field="email_verified",
        name_field="name", avatar_field="picture",
    ),
    "microsoft": ProviderSpec(
        name="microsoft",
        authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        userinfo_url="https://graph.microsoft.com/oidc/userinfo",
        scopes="openid email profile",
        is_oidc=True,
        subject_field="sub", email_field="email", verified_field="email_verified",
        name_field="name", avatar_field="picture",
    ),
    "facebook": ProviderSpec(
        name="facebook",
        authorize_url="https://www.facebook.com/v18.0/dialog/oauth",
        token_url="https://graph.facebook.com/v18.0/oauth/access_token",
        userinfo_url="https://graph.facebook.com/me?fields=id,name,email",
        scopes="email public_profile",
        is_oidc=False,
        # Facebook does not assert email verification; treated as unverified.
        subject_field="id", email_field="email", verified_field=None,
        name_field="name", avatar_field=None,
    ),
}

ALL_PROVIDERS = tuple(_SPECS)


@runtime_checkable
class OAuthProvider(Protocol):
    name: str

    def authorization_url(self, *, state: str, nonce: str | None, redirect_uri: str) -> str: ...

    def fetch_identity(
        self, *, code: str, state: str, nonce: str | None, redirect_uri: str
    ) -> ExternalIdentityInfo: ...


def _credentials(settings, name: str) -> tuple[str | None, str | None]:
    return (
        getattr(settings, f"{name}_client_id", None),
        getattr(settings, f"{name}_client_secret", None),
    )


def is_provider_enabled(settings, name: str) -> bool:
    if name not in _SPECS:
        return False
    client_id, client_secret = _credentials(settings, name)
    return bool(client_id and client_secret)


def enabled_provider_names(settings) -> list[str]:
    return [name for name in ALL_PROVIDERS if is_provider_enabled(settings, name)]


class AuthlibProvider:
    """Real provider backed by Authlib. Instantiated only for enabled
    providers; network happens in fetch_identity (production), never in tests."""

    def __init__(self, spec: ProviderSpec, client_id: str, client_secret: str) -> None:
        self._spec = spec
        self._client_id = client_id
        self._client_secret = client_secret
        self.name = spec.name

    def _session(self, redirect_uri: str):
        from authlib.integrations.requests_client import OAuth2Session

        return OAuth2Session(
            self._client_id,
            self._client_secret,
            scope=self._spec.scopes,
            redirect_uri=redirect_uri,
        )

    def authorization_url(self, *, state: str, nonce: str | None, redirect_uri: str) -> str:
        session = self._session(redirect_uri)
        kwargs = {"state": state}
        if self._spec.is_oidc and nonce:
            kwargs["nonce"] = nonce
        url, _ = session.create_authorization_url(self._spec.authorize_url, **kwargs)
        return url

    def fetch_identity(
        self, *, code: str, state: str, nonce: str | None, redirect_uri: str
    ) -> ExternalIdentityInfo:
        session = self._session(redirect_uri)
        try:
            session.fetch_token(
                self._spec.token_url, code=code, state=state,
                client_secret=self._client_secret,
            )
            resp = session.get(self._spec.userinfo_url)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - surface as a provider error
            raise ProviderError(f"{self.name} token/userinfo exchange failed: {exc}") from exc
        return _info_from_userinfo(self._spec, data)


def _info_from_userinfo(spec: ProviderSpec, data: dict) -> ExternalIdentityInfo:
    subject = data.get(spec.subject_field)
    if not subject:
        raise ProviderError(f"{spec.name} userinfo missing subject")
    verified = bool(data.get(spec.verified_field)) if spec.verified_field else False
    return ExternalIdentityInfo(
        provider=spec.name,
        subject=str(subject),
        email=data.get(spec.email_field),
        email_verified=verified,
        display_name=data.get(spec.name_field),
        avatar_url=data.get(spec.avatar_field) if spec.avatar_field else None,
    )


def build_provider(settings, name: str) -> OAuthProvider:
    if not is_provider_enabled(settings, name):
        raise ProviderError(f"provider '{name}' is not enabled")
    client_id, client_secret = _credentials(settings, name)
    return AuthlibProvider(_SPECS[name], client_id, client_secret)


class MockProvider:
    """Test double. Returns a preset identity and records calls; no network."""

    def __init__(self, info: ExternalIdentityInfo) -> None:
        self.name = info.provider
        self._info = info
        self.authorize_calls: list[dict] = []

    def authorization_url(self, *, state: str, nonce: str | None, redirect_uri: str) -> str:
        self.authorize_calls.append({"state": state, "nonce": nonce, "redirect_uri": redirect_uri})
        return f"https://mock-provider.test/authorize?state={state}"

    def fetch_identity(
        self, *, code: str, state: str, nonce: str | None, redirect_uri: str
    ) -> ExternalIdentityInfo:
        return self._info
