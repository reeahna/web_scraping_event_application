"""Load college towns and their event sources from files in the repository.

`data/college_towns/cities.csv` lists the towns, and every other CSV in that
folder is one day's sources in the onboarding upload format (url, name,
source_display_name, city_slug, timezone). On each run, a listed town whose
slug does not exist yet is created, and each source file not imported before
is queued as an onboarding batch. The scheduler's onboarding tick then
processes those jobs exactly as it does an admin's CSV upload.

A file counts as imported once a batch carries its name as correlation id, so
a restart or redeploy never queues it twice. A file naming a town that does not
exist yet is left for a later run rather than queued with rejected rows.
Existing towns are never changed: a town created or edited in the admin wins.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.auto_onboarding import ACTOR_SYSTEM
from app.core.logging import get_logger
from app.core.onboarding_jobs import SOURCE_CSV
from app.models.onboarding_batch import OnboardingBatch
from app.repositories.city import create_city, get_city_by_name, get_city_by_slug
from app.schemas.city import CityCreate
from app.services.audit import record_audit
from app.services.bulk_onboarding import create_batch_from_submission
from app.services.onboarding_submission import SubmissionLimits, parse_csv

logger = get_logger("college_towns")

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "college_towns"
CITIES_FILE = "cities.csv"
CORRELATION_PREFIX = "college-towns:"


@dataclass
class ImportSummary:
    cities_created: list[str] = field(default_factory=list)
    files_queued: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _limits() -> SubmissionLimits:
    settings = get_settings()
    return SubmissionLimits(
        max_urls=settings.onboarding_max_urls_per_batch,
        max_csv_rows=settings.onboarding_max_csv_rows,
        max_csv_bytes=settings.onboarding_max_csv_bytes,
        max_url_length=settings.onboarding_max_url_length,
    )


def _create_missing_cities(db: Session, path: Path, summary: ImportSummary) -> None:
    rows = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig")))
    for row in rows:
        try:
            data = CityCreate(
                name=row.get("name") or "",
                slug=row.get("slug") or "",
                state_or_region=(row.get("state_or_region") or "").strip() or None,
                universities=(row.get("universities") or "").split(";"),
                timezone=(row.get("timezone") or "").strip() or "UTC",
            )
        except ValidationError as exc:
            summary.skipped.append(f"{CITIES_FILE}: {row.get('slug')!r} is invalid ({exc})")
            continue
        if get_city_by_slug(db, data.slug) is not None:
            continue
        if get_city_by_name(db, data.name) is not None:
            summary.skipped.append(
                f"{CITIES_FILE}: '{data.name}' already exists with another slug than "
                f"'{data.slug}'"
            )
            continue
        city = create_city(db, data)
        record_audit(
            db,
            actor_id=None,
            action="city_created",
            entity_type="city",
            entity_id=city.id,
            after=data.model_dump(mode="json"),
            correlation_id=f"{CORRELATION_PREFIX}{CITIES_FILE}",
            actor_type=ACTOR_SYSTEM,
        )
        summary.cities_created.append(data.slug)


def _already_imported(db: Session, correlation_id: str) -> bool:
    return (
        db.scalar(
            select(OnboardingBatch.id).where(OnboardingBatch.correlation_id == correlation_id)
        )
        is not None
    )


def import_college_towns(db: Session, directory: Path = DATA_DIR) -> ImportSummary:
    summary = ImportSummary()
    if not directory.is_dir():
        return summary

    cities_path = directory / CITIES_FILE
    if cities_path.is_file():
        _create_missing_cities(db, cities_path, summary)

    limits = _limits()
    for path in sorted(directory.glob("*.csv")):
        if path.name == CITIES_FILE:
            continue
        correlation_id = f"{CORRELATION_PREFIX}{path.name}"[:64]
        if _already_imported(db, correlation_id):
            continue
        parsed = parse_csv(path.read_bytes(), limits)
        if parsed.error:
            summary.skipped.append(f"{path.name}: {parsed.error}")
            continue
        missing = sorted(
            {
                row.city_slug
                for row in parsed.rows
                if row.city_slug and get_city_by_slug(db, row.city_slug) is None
            }
        )
        if missing:
            summary.skipped.append(f"{path.name}: unknown city slug {', '.join(missing)}")
            continue
        create_batch_from_submission(
            db,
            parsed,
            submitted_by_user_id=None,
            default_city_id=None,
            default_timezone=None,
            redetect_existing=False,
            source_kind=SOURCE_CSV,
            correlation_id=correlation_id,
        )
        summary.files_queued.append(path.name)

    for reason in summary.skipped:
        logger.warning("college town import skipped %s", reason)
    return summary
