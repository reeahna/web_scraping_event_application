"""Token-saving prompt options and the side-by-side model evaluation. No
network: categorizers are fakes or run against an httpx MockTransport."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models.ai_label_evaluation import AiLabelEvaluation
from app.models.event import Event
from app.models.event_category import EventCategory
from app.services import ai_label_eval
from app.services.ai_categorization import estimate_tokens, fit_to_token_budget
from app.services.ai_categorizer import (
    CallStats,
    CategoryOption,
    EventLabel,
    EventToClassify,
    GeminiQuotaExceeded,
    GroqCategorizer,
    SplitPrompt,
    build_event_block,
    build_instructions,
    build_prompt,
)

SOON = date.today() + timedelta(days=3)
CATEGORIES = [CategoryOption("music", "Music"), CategoryOption("other", "Other")]


# --- Prompt options -----------------------------------------------------------


def test_default_prompt_is_unchanged():
    events = [EventToClassify(1, "Jazz", description="x" * 900, venue="Hall")]
    prompt = build_prompt(CATEGORIES, events)
    assert prompt == build_instructions(CATEGORIES) + build_event_block(events)
    assert prompt.startswith("You are labeling events")
    # 600 characters plus the ellipsis, as before.
    assert ("x" * 600 + "…") in prompt and ("x" * 601) not in prompt
    payload = GroqCategorizer("k", "openai/gpt-oss-120b")._payload(prompt)
    assert payload["messages"][1] == {"role": "user", "content": prompt}


def test_shorter_descriptions_and_split_layout():
    categorizer = GroqCategorizer(
        "k", "openai/gpt-oss-120b", description_chars=300, prompt_layout="split"
    )
    events = [EventToClassify(1, "Jazz", description="y" * 900)]
    prompt = categorizer._build_prompt(CATEGORIES, events)
    assert isinstance(prompt, SplitPrompt)
    assert ("y" * 300 + "…") in prompt.events and ("y" * 301) not in prompt.events
    payload = categorizer._payload(prompt)
    system, user = payload["messages"]
    # The fixed instructions lead the request, identical for every batch.
    assert system["content"].startswith(build_instructions(CATEGORIES))
    assert user["content"].startswith("Events:")
    other = categorizer._payload(categorizer._build_prompt(CATEGORIES, [EventToClassify(2, "B")]))
    assert other["messages"][0] == system


def test_unknown_layout_is_refused():
    with pytest.raises(ValueError):
        GroqCategorizer("k", "m", prompt_layout="fancy")


def test_batches_are_cut_to_the_token_budget():
    categorizer = GroqCategorizer("k", "m")
    events = [EventToClassify(i, f"Event {i}", description="z" * 600) for i in range(40)]
    fitted = fit_to_token_budget(categorizer, CATEGORIES, events, 2000)
    assert 1 <= len(fitted) < 40
    assert estimate_tokens(build_prompt(CATEGORIES, fitted)) <= 2000
    assert fit_to_token_budget(categorizer, CATEGORIES, events[:1], 10) == events[:1]


# --- Evaluation -----------------------------------------------------------------


@pytest.fixture
def groq(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test")
    monkeypatch.setattr(settings, "gemini_scheduled_enabled", True)


def _seed(make_city, make_event, db_session):
    city = make_city()
    music = db_session.query(EventCategory).filter_by(slug="music").one()
    titles = [
        "Symphony Orchestra Concert", "Pottery Workshop", "Chess Club Meeting",
        "Physics Colloquium Lecture", "Faculty Senate Meeting", "Zoom Webinar on Taxes",
        "Thing", "Jazz Night",
    ]
    events = [
        make_event(city, title=t, canonical_url=f"https://x/{i}", start_date=SOON,
                   description="A long enough description of the event for sampling." * 2)
        for i, t in enumerate(titles)
    ]
    make_event(city, title="Admin Fixed", canonical_url="https://x/o", start_date=SOON,
               category_override_id=music.id)
    make_event(city, title="Hidden By AI", canonical_url="https://x/h", start_date=SOON,
               is_active=False, category_source="ai")
    make_event(city, title="Long Past", canonical_url="https://x/p",
               start_date=date.today() - timedelta(days=200))
    return events


def test_sample_spreads_over_kinds_of_event(db_session, make_city, make_event):
    _seed(make_city, make_event, db_session)
    sample = ai_label_eval.select_sample(db_session, size=100)
    titles = {item["title"] for item in sample}
    assert "Long Past" not in titles
    assert {"Admin Fixed", "Hidden By AI", "Faculty Senate Meeting"} <= titles
    buckets = {b for item in sample for b in item["buckets"]}
    assert {"performances", "internal_meetings", "online", "admin_override",
            "hidden_by_ai", "little_information"} <= buckets
    fixed = next(item for item in sample if item["title"] == "Admin Fixed")
    assert fixed["override_category"] == "music"


def test_evaluation_needs_a_groq_key(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "groq_api_key", None)
    with pytest.raises(ValueError):
        ai_label_eval.start_evaluation(db_session, user_id=None)


class FakeCategorizer:
    """Answers like `answer(config, events)` and reports fixed token use."""

    def __init__(self, config, answer, quota_models=()):
        self.config = config
        self.answer = answer
        self.quota_models = quota_models
        self.last_stats = CallStats(model=config["model"])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def classify_batch(self, options, events):
        if self.config["model"] in self.quota_models:
            raise GeminiQuotaExceeded("HTTP 429", 600.0, "TPD")
        self.last_stats.total_tokens = 100 * len(events)
        self.last_stats.prompt_tokens = 90 * len(events)
        self.last_stats.cached_tokens = 10 * len(events)
        return self.answer(self.config, events)


def _answer(config, events):
    labels = {}
    for event in events:
        keep = True
        # The candidate model hides the chess club: a false hide.
        if config["model"] == "openai/gpt-oss-20b" and "Chess" in event.title:
            keep = False
        if "Senate" in event.title:
            keep = False
        labels[event.id] = EventLabel("music" if "Jazz" in event.title else "other", keep)
    return labels


def test_evaluation_runs_to_a_report_without_touching_events(
    db_session, groq, make_city, make_event
):
    _seed(make_city, make_event, db_session)
    before = {e.id: (e.category_id, e.category_source, e.is_active)
              for e in db_session.query(Event)}
    evaluation = ai_label_eval.start_evaluation(db_session, user_id=None)
    db_session.commit()
    assert [c["key"] for c in evaluation.configs] == [
        "baseline", "lean", "candidate", "candidate_lean"]

    # The main model is rate limited first: the candidate keeps going meanwhile.
    now = datetime.now(UTC)
    outcome = ai_label_eval.run_evaluation_step(
        SessionLocal, now=now,
        categorizer_factory=lambda c: FakeCategorizer(c, _answer, {"openai/gpt-oss-120b"}),
    )
    assert outcome == "rate_limited"
    outcome = ai_label_eval.run_evaluation_step(
        SessionLocal, now=now,
        categorizer_factory=lambda c: FakeCategorizer(c, _answer),
    )
    assert outcome == "ok"
    db_session.expire_all()
    progress = db_session.get(AiLabelEvaluation, evaluation.id).progress
    assert progress["candidate"]["done"] == [0]
    assert progress["baseline"]["done"] == []

    later = now + timedelta(hours=1)
    for _ in range(20):
        outcome = ai_label_eval.run_evaluation_step(
            SessionLocal, now=later, categorizer_factory=lambda c: FakeCategorizer(c, _answer)
        )
        if outcome == "finished":
            break
    assert outcome == "finished"
    assert ai_label_eval.run_evaluation_step(SessionLocal) is None

    db_session.expire_all()
    done = db_session.get(AiLabelEvaluation, evaluation.id)
    report = done.report
    assert done.status == "done"
    lean = report["candidates"][0]
    assert lean["config"]["key"] == "lean"
    assert lean["category_agreement_pct"] == 100.0 and lean["false_hides"] == 0
    candidate = report["candidates"][1]
    assert candidate["false_hides"] == 1
    assert candidate["meets_95_pct"] is False
    assert [d["title"] for d in candidate["disagreements"]] == ["Chess Club Meeting"]
    assert candidate["tokens_per_event"] == 100
    assert report["baseline"]["hidden"] == 1
    assert report["baseline"]["override_accuracy"] == {"events": 1, "matching": 0, "pct": 0.0}

    after = {e.id: (e.category_id, e.category_source, e.is_active)
             for e in db_session.query(Event)}
    assert after == before


@pytest.fixture
def admin_client(client, make_super_admin, login):
    make_super_admin(email="eval-root@example.com")
    login("eval-root@example.com")
    return client


def test_admin_starts_views_and_stops_an_evaluation(
    admin_client, db_session, groq, make_city, make_event
):
    _seed(make_city, make_event, db_session)
    csrf = admin_client.cookies.get("csrf_token")
    resp = admin_client.post("/admin/ai-labeling/evaluations", data={"csrf_token": csrf},
                             follow_redirects=False)
    assert resp.status_code == 303
    evaluation_id = int(resp.headers["location"].rsplit("/", 1)[1])

    again = admin_client.post("/admin/ai-labeling/evaluations", data={"csrf_token": csrf},
                              follow_redirects=False)
    assert again.status_code == 409

    page = admin_client.get(f"/admin/ai-labeling/evaluations/{evaluation_id}")
    assert page.status_code == 200 and "Progress" in page.text
    data = admin_client.get(f"/admin/ai-labeling/evaluations/{evaluation_id}/data").json()
    assert data["status"] == "running" and len(data["progress"]) == 4
    assert "Start an evaluation" not in admin_client.get("/admin/ai-labeling").text

    resp = admin_client.post(f"/admin/ai-labeling/evaluations/{evaluation_id}/cancel",
                             data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    assert db_session.get(AiLabelEvaluation, evaluation_id).status == "cancelled"


def test_finished_report_page_renders(admin_client, db_session, groq, make_city, make_event):
    _seed(make_city, make_event, db_session)
    evaluation = ai_label_eval.start_evaluation(db_session, user_id=None)
    db_session.commit()
    for _ in range(20):
        if ai_label_eval.run_evaluation_step(
            SessionLocal, categorizer_factory=lambda c: FakeCategorizer(c, _answer)
        ) == "finished":
            break
    page = admin_client.get(f"/admin/ai-labeling/evaluations/{evaluation.id}")
    assert page.status_code == 200
    assert "Chess Club Meeting" in page.text
    assert "Would hide what the baseline keeps" in page.text
