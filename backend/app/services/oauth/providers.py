"""OAuth/OIDC provider adapters.

`AuthlibProvider` uses Authlib's OAuth2 client for the real authorization-URL
build, code->token exchange, and userinfo fetch — the protocol is never
implemented by hand here. Each provider is described by a `ProviderSpec`
(endpoints + scopes + how to read its userinfo). `MockProvider` returns a
canned identity for tests without any network.
"""

from __future__ import annotations

import hashlib
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
    # How the client secret is sent to the token endpoint. Most providers take
    # it in the form body; X only accepts an HTTP Basic header.
    token_auth_method: str = "client_secret_post"
    # PKCE (S256). X requires it on every authorization-code request.
    pkce: bool = False
    # Some APIs wrap the profile in an envelope (X: {"data": {...}}).
    userinfo_key: str | None = None


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
    "yahoo": ProviderSpec(
        name="yahoo",
        authorize_url="https://api.login.yahoo.com/oauth2/request_auth",
        token_url="https://api.login.yahoo.com/oauth2/get_token",
        userinfo_url="https://api.login.yahoo.com/openid/v1/userinfo",
        scopes="openid email profile",
        is_oidc=True,
        # Yahoo does send email_verified, but it is not read: like Microsoft
        # and Facebook, a Yahoo sign-in never links to an existing account by
        # email or claims SUPERADMIN_EMAIL. Only Google is trusted for that.
        subject_field="sub", email_field="email", verified_field=None,
        name_field="name", avatar_field="picture",
    ),
    "x": ProviderSpec(
        name="x",
        authorize_url="https://x.com/i/oauth2/authorize",
        token_url="https://api.x.com/2/oauth2/token",
        # users.email (with confirmed_email) gives the address; tweet.read is
        # required alongside users.read to call /2/users/me.
        userinfo_url="https://api.x.com/2/users/me?user.fields=confirmed_email,profile_image_url",
        scopes="tweet.read users.read users.email",
        is_oidc=False,
        # X reports only a "confirmed" address, which like Facebook's is not
        # trusted to link to an existing account or claim SUPERADMIN_EMAIL.
        subject_field="id", email_field="confirmed_email", verified_field=None,
        name_field="name", avatar_field="profile_image_url",
        token_auth_method="client_secret_basic",
        pkce=True,
        userinfo_key="data",
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
            # Send the secret once, in one place. Facebook only accepts it in
            # the form body, X only in a Basic header, and Google rejects a
            # request that carries it in both.
            token_endpoint_auth_method=self._spec.token_auth_method,
            code_challenge_method="S256" if self._spec.pkce else None,
        )

    def authorization_url(self, *, state: str, nonce: str | None, redirect_uri: str) -> str:
        session = self._session(redirect_uri)
        kwargs = {"state": state}
        if self._spec.is_oidc and nonce:
            kwargs["nonce"] = nonce
        if self._spec.pkce:
            kwargs["code_verifier"] = _pkce_verifier(nonce)
        url, _ = session.create_authorization_url(self._spec.authorize_url, **kwargs)
        return url

    def fetch_identity(
        self, *, code: str, state: str, nonce: str | None, redirect_uri: str
    ) -> ExternalIdentityInfo:
        session = self._session(redirect_uri)
        try:
            token_kwargs = {"code": code, "state": state}
            if self._spec.pkce:
                token_kwargs["code_verifier"] = _pkce_verifier(nonce)
            session.fetch_token(self._spec.token_url, **token_kwargs)
            resp = session.get(self._spec.userinfo_url)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - surface as a provider error
            raise ProviderError(f"{self.name} token/userinfo exchange failed: {exc}") from exc
        return _info_from_userinfo(self._spec, data)


def _pkce_verifier(nonce: str | None) -> str:
    """The PKCE code verifier for one sign-in, derived from its nonce. The
    nonce is random, stored server-side with the one-time state and never sent
    to a non-OIDC provider, so the verifier needs no storage of its own and
    is the same when the person comes back. 64 hex characters fits the
    43-128 character rule."""
    if not nonce:
        raise ProviderError("PKCE sign-in needs a nonce")
    return hashlib.sha256(nonce.encode()).hexdigest()


def _info_from_userinfo(spec: ProviderSpec, data: dict) -> ExternalIdentityInfo:
    if spec.userinfo_key:
        data = data.get(spec.userinfo_key) or {}
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
