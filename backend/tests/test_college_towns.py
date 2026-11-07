"""Importing college towns and their source CSVs from data/college_towns/."""

from __future__ import annotations

from app.core.onboarding_jobs import QUEUED
from app.models.onboarding_batch import OnboardingBatch
from app.models.onboarding_job import OnboardingJob
from app.repositories.city import get_city_by_slug
from app.schemas.city import CityCreate
from app.services.college_towns import DATA_DIR, import_college_towns
from app.services.onboarding_submission import SubmissionLimits, parse_csv

CITIES = (
    "name,slug,state_or_region,universities,timezone\n"
    'Eugene,eugene-or,Oregon,"University of Oregon; Lane Community College",'
    "America/Los_Angeles\n"
)
SOURCES = (
    "url,name,source_display_name,city_slug,timezone\n"
    "https://calendar.example.edu/,Eugene - Calendar,Calendar,eugene-or,America/Los_Angeles\n"
    "https://www.example.org/events,Eugene - Hall,Hall,eugene-or,America/Los_Angeles\n"
)


def _write(directory, files: dict[str, str]):
    directory.mkdir(exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


def test_creates_city_and_queues_its_sources(db_session, tmp_path):
    folder = _write(tmp_path / "towns", {"cities.csv": CITIES, "11-eugene-or.csv": SOURCES})

    summary = import_college_towns(db_session, folder)

    assert summary.cities_created == ["eugene-or"]
    assert summary.files_queued == ["11-eugene-or.csv"]
    city = get_city_by_slug(db_session, "eugene-or")
    assert city.name == "Eugene"
    assert city.state_or_region == "Oregon"
    assert city.timezone == "America/Los_Angeles"
    assert [u.name for u in city.universities] == [
        "Lane Community College",
        "University of Oregon",
    ]
    jobs = db_session.query(OnboardingJob).all()
    assert {job.submitted_url for job in jobs} == {
        "https://calendar.example.edu/",
        "https://www.example.org/events",
    }
    assert {job.status for job in jobs} == {QUEUED}
    assert {job.city_id for job in jobs} == {city.id}


def test_running_again_queues_nothing_new(db_session, tmp_path):
    folder = _write(tmp_path / "towns", {"cities.csv": CITIES, "11-eugene-or.csv": SOURCES})
    import_college_towns(db_session, folder)

    summary = import_college_towns(db_session, folder)

    assert summary.cities_created == []
    assert summary.files_queued == []
    assert db_session.query(OnboardingBatch).count() == 1


def test_existing_city_is_left_as_it_is(db_session, make_city, tmp_path):
    make_city(name="Eugene, OR", slug="eugene-or", timezone="America/Los_Angeles")
    folder = _write(tmp_path / "towns", {"cities.csv": CITIES, "11-eugene-or.csv": SOURCES})

    summary = import_college_towns(db_session, folder)

    assert summary.cities_created == []
    assert get_city_by_slug(db_session, "eugene-or").name == "Eugene, OR"
    assert summary.files_queued == ["11-eugene-or.csv"]


def test_name_taken_by_another_slug_is_skipped(db_session, make_city, tmp_path):
    make_city(name="Eugene", slug="eugene")
    folder = _write(tmp_path / "towns", {"cities.csv": CITIES, "11-eugene-or.csv": SOURCES})

    summary = import_college_towns(db_session, folder)

    assert get_city_by_slug(db_session, "eugene-or") is None
    assert summary.files_queued == []
    assert any("unknown city slug eugene-or" in reason for reason in summary.skipped)
    assert db_session.query(OnboardingBatch).count() == 0


def test_file_waits_until_its_city_exists(db_session, tmp_path):
    folder = _write(tmp_path / "towns", {"11-eugene-or.csv": SOURCES})

    assert import_college_towns(db_session, folder).files_queued == []

    _write(folder, {"cities.csv": CITIES})
    assert import_college_towns(db_session, folder).files_queued == ["11-eugene-or.csv"]


def test_missing_folder_does_nothing(db_session, tmp_path):
    summary = import_college_towns(db_session, tmp_path / "nowhere")

    assert summary.cities_created == summary.files_queued == summary.skipped == []


def test_committed_files_are_valid():
    """Every committed source file parses cleanly and names a listed town."""
    import csv

    limits = SubmissionLimits(
        max_urls=200, max_csv_rows=200, max_csv_bytes=1_000_000, max_url_length=2000
    )
    with (DATA_DIR / "cities.csv").open(encoding="utf-8") as handle:
        cities = list(csv.DictReader(handle))
    for row in cities:
        CityCreate(**{**row, "universities": row["universities"].split(";")})
    slugs = {row["slug"] for row in cities}
    assert len({row["name"] for row in cities}) == len(cities)
    files = [p for p in DATA_DIR.glob("*.csv") if p.name != "cities.csv"]
    assert files
    for path in files:
        parsed = parse_csv(path.read_bytes(), limits)
        assert parsed.error is None, path.name
        assert parsed.rejected == [], path.name
        assert {row.city_slug for row in parsed.rows} <= slugs, path.name
