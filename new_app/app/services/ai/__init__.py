"""Optional AI configuration assistant.

AI is advisory only. It may *suggest* a SiteConfiguration when deterministic
inference could not, but it never scrapes, approves, activates, persists
events, or runs during recurring extraction. Everything here is a no-op when
AI is disabled, which is the default, so the application is fully functional
with no provider configured and automated tests never touch a network.
"""

from app.services.ai.provider import (
    AIProviderError,
    AIProviderUnavailable,
    get_ai_provider,
)
from app.services.ai.types import (
    AIConfigurationEvidence,
    AISuggestionRequest,
    AISuggestionResult,
    AIUsageStatus,
)

__all__ = [
    "AIConfigurationEvidence",
    "AIProviderError",
    "AIProviderUnavailable",
    "AISuggestionRequest",
    "AISuggestionResult",
    "AIUsageStatus",
    "get_ai_provider",
]
