"""Email delivery interface (Phase 13).

An interface plus safe built-in backends. The default is ``NoopEmailSender``,
which sends nothing and reports it — so no real email is ever sent unless email
is explicitly enabled AND a real backend is configured. A real SMTP/API sender
would implement the same ``EmailSender`` protocol behind explicit credentials
(a deliberately deferred, credential-gated step).
"""

from __future__ import annotations

from dataclasses import dataclass
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
    """Logs the message instead of sending — useful in development."""

    name = "console"

    def send(self, message: EmailMessage) -> bool:
        logger.info("EMAIL to=%s subject=%s", message.to, message.subject)
        return True


class MemoryEmailSender:
    """Test double: records messages in memory. Never touches the network."""

    name = "memory"

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> bool:
        self.sent.append(message)
        return True


def get_email_sender(settings) -> EmailSender:
    if not settings.email_enabled:
        return NoopEmailSender()
    if settings.email_backend == "console":
        return ConsoleEmailSender()
    return NoopEmailSender()
