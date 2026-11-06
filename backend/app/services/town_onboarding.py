"""Onboard a whole college town with Claude.

The manual routine this replaces: for each new town, search for the event
listing pages on campus and in town, write a source CSV
(url,name,source_display_name,city_slug,timezone), create the city, then upload
the CSV under Onboarding. Here an administrator types the town, and:

1. `discover_town` asks Claude, with web search and web fetch, for the town's
   city record and at least `town_onboarding_min_sources` listing pages, some
   on campus and some in town. Claude answers by calling a `submit_town` tool
   whose input is checked here; a proposal that falls short (too few pages,
   a bad timezone, a social-media page) goes back to Claude with the problems
   listed, and it searches again.
2. `apply_proposal` creates the city when no city with that slug or name
   exists (an existing one is used as it is), then turns the sources into the
   same CSV the manual routine produced and queues it through
   `create_batch_from_submission`. From there each source goes through the
   ordinary onboarding pipeline: detection, preview, policy, review. Nothing
   is approved or activated here.

Requests are rows in `town_onboarding_requests`, drained one at a time by the
scheduler process (`process_next_request`), because a town takes Claude a few
minutes of searching — far too long for a web request.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.onboarding_jobs import SOURCE_CLAUDE
from app.core.url_canonical import canonical_url
from app.core.url_safety import UnsafeURLError, validate_public_url
from app.models.city import City
from app.models.onboarding_batch import OnboardingBatch
from app.models.town_onboarding import (
    TOWN_DONE,
    TOWN_FAILED,
    TOWN_QUEUED,
    TOWN_RUNNING,
    TownOnboardingRequest,
)
from app.repositories.city import create_city, get_city_by_name, get_city_by_slug
from app.schemas.city import _VALID_TIMEZONES, CityCreate
from app.services.audit import record_audit
from app.services.bulk_onboarding import create_batch_from_submission
from app.services.onboarding_submission import SubmissionLimits, parse_csv

logger = get_logger("town_onboarding")

CAMPUS = "campus"
TOWN = "town"
SCOPES = (CAMPUS, TOWN)

CSV_COLUMNS = ("url", "name", "source_display_name", "city_slug", "timezone")

# Pages that are never a town's own listing: sign-in walls, feeds of
# everything everywhere, or ticket resellers. Claude is told the same; this
# is the check that it listened.
_EXCLUDED_HOSTS = (
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "linkedin.com",
    "eventbrite.com",
    "ticketmaster.com",
    "livenation.com",
    "stubhub.com",
    "seatgeek.com",
    "vividseats.com",
    "allevents.in",
    "meetup.com",
    "google.com",
    "yelp.com",
    "tripadvisor.com",
)

_STATE_CODE_RE = re.compile(r"^[A-Za-z]{2}$")
_SLUG_PART_RE = re.compile(r"[^a-z0-9]+")
_MAX_ROUNDS = 3  # proposals Claude may resubmit after being told what was wrong
_MAX_TURNS = 12  # API calls for one town, counting pause_turn resumes


class TownOnboardingError(Exception):
    """Discovery could not produce an acceptable proposal."""


class TownOnboardingNotConfigured(TownOnboardingError):
    """No ANTHROPIC_API_KEY is set."""


# --- the proposal -----------------------------------------------------------


@dataclass
class ProposedSource:
    url: str
    display_name: str
    scope: str
    reason: str = ""


@dataclass
class TownProposal:
    city_name: str
    state_or_region: str
    state_code: str
    country: str
    timezone: str
    latitude: float | None
    longitude: float | None
    universities: list[str]
    sources: list[ProposedSource]
    # Pages Claude suggested that were dropped, with why.
    rejected: list[dict[str, str]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return city_slug(self.city_name, self.state_code)

    def count(self, scope: str) -> int:
        return sum(1 for source in self.sources if source.scope == scope)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["slug"] = self.slug
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TownProposal:
        values = {k: v for k, v in data.items() if k != "slug"}
        values["sources"] = [ProposedSource(**s) for s in data.get("sources") or []]
        return cls(**values)


def city_slug(city_name: str, state_code: str) -> str:
    """'Ann Arbor', 'MI' -> 'ann-arbor-mi', the format every town already uses."""
    base = _SLUG_PART_RE.sub("-", city_name.lower()).strip("-")
    return f"{base}-{state_code.lower()}" if state_code else base


def _host_excluded(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(host == bad or host.endswith("." + bad) for bad in _EXCLUDED_HOSTS)


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def check_proposal(
    raw: dict[str, Any], *, min_sources: int, min_per_scope: int
) -> tuple[TownProposal | None, list[str]]:
    """Turns Claude's `submit_town` input into a proposal and lists every
    problem with it. Bad individual pages are dropped (and recorded) rather
    than failing the whole town; the proposal only fails when what is left
    is too little."""
    problems: list[str] = []
    city_name = " ".join(str(raw.get("city_name") or "").split())
    state_code = str(raw.get("state_code") or "").strip().upper()
    timezone = str(raw.get("timezone") or "").strip()
    if not city_name:
        problems.append("city_name is empty.")
    if not _STATE_CODE_RE.fullmatch(state_code):
        problems.append(f"state_code '{state_code}' is not a two-letter code.")
    if timezone not in _VALID_TIMEZONES:
        problems.append(f"timezone '{timezone}' is not an IANA timezone name.")

    sources: list[ProposedSource] = []
    rejected: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw.get("sources") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        scope = str(item.get("scope") or "").strip().lower()
        name = " ".join(str(item.get("display_name") or "").split())[:200]
        reason = None
        try:
            url = validate_public_url(url)
        except UnsafeURLError as exc:
            reason = str(exc)
        if reason is None and _host_excluded(url):
            reason = "social media, ticketing and nationwide aggregator sites are not used"
        if reason is None and scope not in SCOPES:
            reason = f"scope must be 'campus' or 'town', not '{scope}'"
        if reason is None and not name:
            reason = "display_name is empty"
        key = canonical_url(url) if reason is None else ""
        if reason is None and (not key or key in seen):
            reason = "listed twice"
        if reason is not None:
            rejected.append({"url": url, "reason": reason})
            continue
        seen.add(key)
        sources.append(
            ProposedSource(
                url=url,
                display_name=name,
                scope=scope,
                reason=" ".join(str(item.get("reason") or "").split())[:300],
            )
        )

    proposal = TownProposal(
        city_name=city_name,
        state_or_region=" ".join(str(raw.get("state_or_region") or "").split()),
        state_code=state_code,
        country=" ".join(str(raw.get("country") or "").split()) or "United States",
        timezone=timezone,
        latitude=_as_float(raw.get("latitude")),
        longitude=_as_float(raw.get("longitude")),
        universities=[
            " ".join(str(u).split()) for u in raw.get("universities") or [] if str(u).strip()
        ],
        sources=sources,
        rejected=rejected,
    )
    for item in rejected:
        problems.append(f"Dropped {item['url']}: {item['reason']}.")
    if len(sources) < min_sources:
        problems.append(
            f"Only {len(sources)} usable source(s); at least {min_sources} are needed."
        )
    for scope in SCOPES:
        if proposal.count(scope) < min_per_scope:
            problems.append(
                f"Only {proposal.count(scope)} {scope} source(s); "
                f"at least {min_per_scope} are needed."
            )
    blocking = [p for p in problems if not p.startswith("Dropped ")]
    return (None if blocking else proposal), problems


# --- asking Claude ----------------------------------------------------------

SYSTEM_PROMPT = """\
You find event sources for Bulletin, a website that shows college students \
what is happening in their college town. Bulletin scrapes each source on a \
schedule, so a source is one web page that lists many upcoming, dated events \
for that town, such as a calendar or an events list.

Look for sources on both sides of town life:
- Campus: the university's main events calendar, the student organization \
platform (for example CampusGroups, Presence or Engage), the student union or \
student activities board, the performing arts center, campus museums, the \
recreation center, and the athletics schedule.
- Town: the city or visitor bureau calendar, the downtown association, the \
public library, local music venues and theaters, the arts council, and the \
local newspaper or alt-weekly calendar.

A good source:
- is the listing page itself, not the home page of the site and not a single \
event;
- lists events happening in this town (not a statewide or national feed);
- is public, with no sign-in;
- is still maintained, with events in the coming weeks;
- if the site offers a list view next to a month grid, is the list view URL.

Do not use Facebook, Instagram, X, TikTok, LinkedIn, Eventbrite, Meetup, \
Ticketmaster or other ticket resellers, AllEvents, Google, Yelp or \
TripAdvisor, and do not use PDFs.

Use web search to find candidates and web fetch to open each one and confirm \
it lists upcoming events before you include it. When you are done, call the \
submit_town tool once with the town's details and every source you confirmed. \
If the tool reports problems, fix them (usually by finding more sources) and \
call it again."""

SUBMIT_TOOL: dict[str, Any] = {
    "name": "submit_town",
    "description": (
        "Submit the college town's city record and its event listing pages. Call this "
        "once you have confirmed the pages with web fetch."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "city_name",
            "state_or_region",
            "state_code",
            "country",
            "timezone",
            "latitude",
            "longitude",
            "universities",
            "sources",
        ],
        "properties": {
            "city_name": {
                "type": "string",
                "description": "The town as people name it, e.g. 'Ann Arbor'.",
            },
            "state_or_region": {
                "type": "string",
                "description": "Full state name, e.g. 'Michigan'.",
            },
            "state_code": {
                "type": "string",
                "description": "Two-letter postal code, e.g. 'MI'.",
            },
            "country": {"type": "string"},
            "timezone": {
                "type": "string",
                "description": "IANA timezone, e.g. 'America/Detroit'.",
            },
            "latitude": {"type": "number", "description": "Town centre latitude."},
            "longitude": {"type": "number", "description": "Town centre longitude."},
            "universities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Full names of the universities in this town.",
            },
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["url", "display_name", "scope", "reason"],
                    "properties": {
                        "url": {"type": "string", "description": "The listing page URL."},
                        "display_name": {
                            "type": "string",
                            "description": "Short name shown to readers, e.g. 'The Ark'.",
                        },
                        "scope": {"type": "string", "enum": list(SCOPES)},
                        "reason": {
                            "type": "string",
                            "description": "One line: what the page lists.",
                        },
                    },
                },
            },
        },
    },
}


def build_request_text(
    town_name: str, state_or_region: str | None, universities: list[str], *, min_sources: int,
    min_per_scope: int,
) -> str:
    where = town_name.strip()
    if state_or_region:
        where += f", {state_or_region.strip()}"
    lines = [f"Find event sources for the college town {where}."]
    if universities:
        lines.append("Its universities: " + "; ".join(universities) + ".")
    lines.append(
        f"Find at least {min_sources} sources, with at least {min_per_scope} on campus and "
        f"at least {min_per_scope} in town. More good sources are welcome, up to about 15."
    )
    return "\n".join(lines)


def _make_client(settings: Settings):
    import anthropic

    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=600.0)


def _add_usage(total: dict[str, int], response: Any) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    for name in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
        value = getattr(usage, name, None)
        if isinstance(value, int):
            total[name] = total.get(name, 0) + value
    server = getattr(usage, "server_tool_use", None)
    for name in ("web_search_requests", "web_fetch_requests"):
        value = getattr(server, name, None) if server is not None else None
        if isinstance(value, int):
            total[name] = total.get(name, 0) + value


def discover_town(
    town_name: str,
    state_or_region: str | None = None,
    universities: list[str] | None = None,
    *,
    settings: Settings | None = None,
    client: Any = None,
) -> TownProposal:
    """Asks Claude for the town's city record and sources. Raises
    TownOnboardingError when no acceptable proposal comes back."""
    settings = settings or get_settings()
    if client is None:
        if not settings.anthropic_api_key:
            raise TownOnboardingNotConfigured("ANTHROPIC_API_KEY is not set.")
        client = _make_client(settings)

    min_sources = settings.town_onboarding_min_sources
    min_per_scope = settings.town_onboarding_min_per_scope
    tools: list[dict[str, Any]] = [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": settings.town_onboarding_max_searches,
            "user_location": {"type": "approximate", "country": "US"},
        },
        {
            "type": "web_fetch_20260209",
            "name": "web_fetch",
            "max_uses": settings.town_onboarding_max_fetches,
        },
        SUBMIT_TOOL,
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": build_request_text(
                town_name,
                state_or_region,
                universities or [],
                min_sources=min_sources,
                min_per_scope=min_per_scope,
            ),
        }
    ]
    usage: dict[str, int] = {}
    rounds = 0
    last_problems: list[str] = []

    for _ in range(_MAX_TURNS):
        response = client.beta.messages.create(
            model=settings.town_onboarding_model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={"effort": settings.town_onboarding_effort},
            tools=tools,
            messages=messages,
            # On a policy decline the API retries on a fallback model itself.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        _add_usage(usage, response)
        if response.stop_reason == "refusal":
            raise TownOnboardingError("Claude declined to research this town.")
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue  # a long server-side search; resend and it carries on

        submit = next(
            (
                block
                for block in response.content
                if getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == "submit_town"
            ),
            None,
        )
        if submit is None:
            if response.stop_reason == "max_tokens":
                raise TownOnboardingError("Claude ran out of room before submitting the town.")
            messages.append(
                {"role": "user", "content": "Please call submit_town with what you found."}
            )
            continue

        raw = submit.input if isinstance(submit.input, dict) else {}
        proposal, problems = check_proposal(
            raw, min_sources=min_sources, min_per_scope=min_per_scope
        )
        if proposal is not None:
            proposal.usage = usage
            return proposal
        rounds += 1
        last_problems = problems
        if rounds > _MAX_ROUNDS:
            break
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": submit.id,
                        "is_error": True,
                        "content": "The proposal was not accepted:\n- "
                        + "\n- ".join(problems)
                        + "\nFix these and call submit_town again with the full list.",
                    }
                ],
            }
        )

    detail = "; ".join(p for p in last_problems if not p.startswith("Dropped "))
    raise TownOnboardingError(
        "Claude did not find enough usable sources" + (f": {detail}" if detail else ".")
    )


# --- turning a proposal into a city and an onboarding batch -----------------


def proposal_csv(
    proposal: TownProposal, *, city_name: str | None = None, slug: str | None = None
) -> str:
    """The source CSV in the same shape as the manual daily files. `city_name`
    and `slug` name an existing city to use instead of the proposed one."""
    city_name = city_name or proposal.city_name
    slug = slug or proposal.slug
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for source in proposal.sources:
        writer.writerow(
            [
                source.url,
                f"{city_name} - {source.display_name}",
                source.display_name,
                slug,
                proposal.timezone,
            ]
        )
    return out.getvalue()


def _limits(settings: Settings) -> SubmissionLimits:
    return SubmissionLimits(
        max_urls=settings.onboarding_max_urls_per_batch,
        max_csv_rows=settings.onboarding_max_csv_rows,
        max_csv_bytes=settings.onboarding_max_csv_bytes,
        max_url_length=settings.onboarding_max_url_length,
    )


def _find_or_create_city(
    db: Session, proposal: TownProposal, *, actor_id: int | None
) -> tuple[City, bool]:
    city = get_city_by_slug(db, proposal.slug) or get_city_by_name(db, proposal.city_name)
    if city is not None:
        return city, False
    data = CityCreate(
        name=proposal.city_name,
        slug=proposal.slug,
        state_or_region=proposal.state_or_region or None,
        universities=proposal.universities,
        country=proposal.country or None,
        timezone=proposal.timezone,
        default_latitude=proposal.latitude,
        default_longitude=proposal.longitude,
        is_active=True,
    )
    city = create_city(db, data)
    record_audit(
        db,
        actor_id=actor_id,
        action="city_created",
        entity_type="city",
        entity_id=city.id,
        after=data.model_dump(mode="json"),
        detail="Created by town onboarding with Claude",
    )
    return city, True


def apply_proposal(
    db: Session,
    proposal: TownProposal,
    *,
    requested_by_user_id: int | None,
    correlation_id: str | None = None,
    settings: Settings | None = None,
) -> tuple[City, OnboardingBatch]:
    """Creates the city if needed and queues every source for onboarding."""
    settings = settings or get_settings()
    city, _created = _find_or_create_city(db, proposal, actor_id=requested_by_user_id)
    # A town that already existed keeps its own name and slug; the rows name it.
    rows = proposal_csv(proposal, city_name=city.name, slug=city.slug)
    parsed = parse_csv(rows.encode("utf-8"), _limits(settings))
    if parsed.error:
        raise TownOnboardingError(parsed.error)
    batch = create_batch_from_submission(
        db,
        parsed,
        submitted_by_user_id=requested_by_user_id,
        default_city_id=city.id,
        default_timezone=None,
        redetect_existing=False,
        source_kind=SOURCE_CLAUDE,
        correlation_id=correlation_id,
    )
    return city, batch


# --- the request queue ------------------------------------------------------


def queue_town(
    db: Session,
    *,
    town_name: str,
    state_or_region: str | None,
    universities: list[str],
    requested_by_user_id: int | None,
) -> TownOnboardingRequest:
    request = TownOnboardingRequest(
        town_name=" ".join(town_name.split())[:255],
        state_or_region=(" ".join((state_or_region or "").split())[:100] or None),
        universities=[u for u in (" ".join(x.split()) for x in universities) if u] or None,
        status=TOWN_QUEUED,
        requested_by_user_id=requested_by_user_id,
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    record_audit(
        db,
        actor_id=requested_by_user_id,
        action="town_onboarding_requested",
        entity_type="town_onboarding_request",
        entity_id=request.id,
        after={"town_name": request.town_name, "state_or_region": request.state_or_region},
    )
    return request


def retry_town(db: Session, request: TownOnboardingRequest) -> TownOnboardingRequest:
    if request.status != TOWN_FAILED:
        return request
    request.status = TOWN_QUEUED
    request.failure_reason = None
    request.completed_at = None
    db.commit()
    return request


def _now() -> datetime:
    return datetime.now(UTC)


Discover = Callable[..., TownProposal]


def process_request(
    db: Session,
    request: TownOnboardingRequest,
    *,
    discover: Discover = discover_town,
    settings: Settings | None = None,
) -> TownOnboardingRequest:
    """Runs one request to a terminal status. Never raises for a discovery
    or apply failure; the reason is stored on the row."""
    settings = settings or get_settings()
    request.status = TOWN_RUNNING
    request.started_at = _now()
    request.attempts = (request.attempts or 0) + 1
    db.commit()

    try:
        proposal = discover(
            request.town_name,
            request.state_or_region,
            list(request.universities or []),
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - every failure lands on the row
        db.rollback()
        logger.warning("town onboarding %s: discovery failed: %s", request.id, exc)
        request.status = TOWN_FAILED
        request.failure_reason = str(exc)[:500] or exc.__class__.__name__
        request.completed_at = _now()
        db.commit()
        return request

    request.proposal = proposal.as_dict()
    db.commit()
    try:
        city, batch = apply_proposal(
            db,
            proposal,
            requested_by_user_id=request.requested_by_user_id,
            correlation_id=f"town-onboarding-{request.id}",
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("town onboarding %s: apply failed: %s", request.id, exc)
        request.status = TOWN_FAILED
        request.failure_reason = f"Could not create the town: {exc}"[:500]
        request.completed_at = _now()
        db.commit()
        return request

    request.city_id = city.id
    request.batch_id = batch.id
    request.status = TOWN_DONE
    request.completed_at = _now()
    db.commit()
    record_audit(
        db,
        actor_id=request.requested_by_user_id,
        action="town_onboarding_completed",
        entity_type="town_onboarding_request",
        entity_id=request.id,
        after={
            "city_id": city.id,
            "batch_id": batch.id,
            "sources": len(proposal.sources),
            "campus": proposal.count(CAMPUS),
            "town": proposal.count(TOWN),
        },
    )
    return request


def process_next_request(
    db: Session, *, discover: Discover = discover_town, settings: Settings | None = None
) -> TownOnboardingRequest | None:
    """Claims the oldest queued request and runs it. One per call, since each
    takes Claude a few minutes."""
    request = db.scalars(
        select(TownOnboardingRequest)
        .where(TownOnboardingRequest.status == TOWN_QUEUED)
        .order_by(TownOnboardingRequest.created_at, TownOnboardingRequest.id)
        .limit(1)
    ).first()
    if request is None:
        return None
    return process_request(db, request, discover=discover, settings=settings)


def list_requests(db: Session, *, limit: int = 50) -> list[TownOnboardingRequest]:
    return list(
        db.scalars(
            select(TownOnboardingRequest)
            .order_by(TownOnboardingRequest.created_at.desc(), TownOnboardingRequest.id.desc())
            .limit(limit)
        )
    )


def proposal_from_request(request: TownOnboardingRequest) -> TownProposal | None:
    if not request.proposal:
        return None
    try:
        return TownProposal.from_dict(request.proposal)
    except (TypeError, ValueError):
        return None


