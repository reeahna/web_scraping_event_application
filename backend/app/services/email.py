"""Email delivery interface (Phase 13).

An interface plus safe built-in backends. The default is ``NoopEmailSender``,
which sends nothing and reports it, so no real email is ever sent unless email
is explicitly enabled AND a real backend is configured. ``SmtpEmailSender``
sends through any SMTP server given by the SMTP_* settings.

Two switches use it: EMAIL_ENABLED for alert emails (``get_email_sender``) and
EMAIL_SIGN_IN_ENABLED for sign-in codes (``get_sign_in_email_sender``), so
either can be on without the other.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, make_msgid
from typing import Protocol, runtime_checkable

from app.core.logging import get_logger

logger = get_logger("email")


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    body: str


@runtime_checkable
class EmailSender(Protocol):
    name: str

    def send(self, message: EmailMessage) -> bool:
        """Return True if the message was actually dispatched."""
        ...


class NoopEmailSender:
    """The default. Never sends; always reports 'not sent'. Guarantees the app
    cannot email anyone until an operator configures a real backend."""

    name = "noop"

    def send(self, message: EmailMessage) -> bool:
        return False


class ConsoleEmailSender:
    """Logs the message instead of sending, body included, so a sign-in code
    can be read from the log in development. Refused for sign-in codes in
    production by the settings."""

    name = "console"

    def send(self, message: EmailMessage) -> bool:
        logger.info(
            "EMAIL to=%s subject=%s body=%s", message.to, message.subject, message.body
        )
        return True


class SmtpEmailSender:
    """Sends through an SMTP server, always over TLS: implicit TLS on port 465,
    STARTTLS on any other port (a server that does not offer it is refused, so
    a password is never sent in the clear)."""

    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        from_address: str,
        from_name: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.from_address = from_address
        self.from_name = from_name
        self.timeout = timeout

    def _connect(self) -> smtplib.SMTP:
        context = ssl.create_default_context()
        if self.port == 465:
            return smtplib.SMTP_SSL(
                self.host, self.port, timeout=self.timeout, context=context
            )
        smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        smtp.starttls(context=context)
        return smtp

    def send(self, message: EmailMessage) -> bool:
        mime = MimeMessage()
        mime["From"] = formataddr((self.from_name or "", self.from_address))
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime["Message-ID"] = make_msgid(domain=self.from_address.rpartition("@")[2] or None)
        mime.set_content(message.body)
        try:
            with self._connect() as smtp:
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(mime)
        except (smtplib.SMTPException, OSError) as exc:
            # The address is not logged: it is personal data, and the failure
            # is about the server, not the recipient.
            logger.warning("SMTP send via %s failed: %s", self.host, type(exc).__name__)
            return False
        return True


class MemoryEmailSender:
    """Test double: records messages in memory. Never touches the network."""

    name = "memory"

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> bool:
        self.sent.append(message)
        return True


def build_email_backend(settings) -> EmailSender:
    """The configured backend, whatever the on/off switches say."""
    if settings.email_backend == "console":
        return ConsoleEmailSender()
    if settings.email_backend == "smtp" and settings.smtp_host:
        return SmtpEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            from_address=settings.email_from,
            from_name=settings.app_name,
            timeout=settings.smtp_timeout_seconds,
        )
    return NoopEmailSender()


def get_email_sender(settings) -> EmailSender:
    """For alert emails: sends only with EMAIL_ENABLED on."""
    if not settings.email_enabled:
        return NoopEmailSender()
    return build_email_backend(settings)


def get_sign_in_email_sender(settings) -> EmailSender:
    """For sign-in codes: sends only with EMAIL_SIGN_IN_ENABLED on."""
    if not settings.email_sign_in_enabled:
        return NoopEmailSender()
    return build_email_backend(settings)


def email_sign_in_available(settings) -> bool:
    """Whether the sign-in page can offer a code by email: switched on, with a
    backend that really sends."""
    return not isinstance(get_sign_in_email_sender(settings), NoopEmailSender)

