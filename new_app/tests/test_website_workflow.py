"""Website admin workflow view model + detail-page rendering.

The view-model tests are pure (SimpleNamespace fakes, no DB) and assert the
single-primary-action rule and step states for every required workflow state.
The rendering tests confirm the detail page returns 200 and organises actions
correctly across all major states, including sparse/missing data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace as NS

from app.services.website_workflow import build_website_workflow

PERMS = {"update": True, "test": True, "approve": True, "delete": True}


def _city(active: bool = True):
    return NS(id=1, name="Bloomington", is_active=active)


def _website(**kw):
    base = dict(
        id=4,
        name="Events this Weekend in Bloomington, IN",
        onboarding_status="draft",
        archived_at=None,
        is_active=False,
        city=_city(),
        configuration=None,
        configuration_version=0,
        active_configuration_version=None,
        proposed_pattern=None,
        approved_pattern=None,
        last_success_at=None,
        last_failure_at=None,
        consecutive_failure_count=0,
    )
    base.update(kw)
    return NS(**base)


def _drafted(pattern="simpleview_events", version=6, status="detected"):
    return _website(
        onboarding_status=status,
        configuration={"pattern_name": pattern},
        configuration_version=version,
        proposed_pattern={"detection": {"pattern_name": pattern}},
    )


def _preview(version, status, valid, found=None, rejected=0):
    return NS(
        id=99,
        configuration_version=version,
        status=status,
        events_valid=valid,
        events_found=found if found is not None else valid,
        events_rejected=rejected,
    )


def _wf(website, *, preview=None, recovery=None, next_states=(), browser=True, perms=PERMS):
    return build_website_workflow(
        website=website,
        latest_preview_run=preview,
        latest_browser_recovery=recovery,
        next_states=list(next_states),
        permissions=perms,
        browser_extraction_enabled=browser,
    )


def _primary(wf):
    return wf.primary_action.label if wf.primary_action else None


def _states(wf):
    return {s.key: s.state for s in wf.steps}


# --- exactly one primary action, correct per state --------------------------


def test_draft_no_detection_primary_is_detect():
    wf = _wf(_website(onboarding_status="draft"), next_states=["needs_review", "archived"])
    assert _primary(wf) == "Detect events automatically"
    assert wf.current_step == "detect"


def test_unsupported_with_browser_recovery_primary_is_retry():
    w = _website(
        onboarding_status="unsupported",
        proposed_pattern={"detection": {"pattern_name": None}},
    )
    wf = _wf(w, next_states=["draft", "needs_review", "archived"])
    assert _primary(wf) == "Retry automatic recovery"


def test_unsupported_browser_disabled_has_no_primary():
    w = _website(
        onboarding_status="unsupported",
        proposed_pattern={"detection": {"pattern_name": None}},
    )
    wf = _wf(w, browser=False, next_states=["draft", "archived"])
    assert wf.primary_action is None
    assert "manually" in wf.explanation.lower()


def test_structured_pattern_needed_website4_state():
    """Website #4 before the next retry: needs_review + failed historical v5 +
    recovery.new_pattern_needed → primary is Retry automatic recovery."""
    w = _website(
        onboarding_status="needs_review",
        configuration={"pattern_name": "generic_html_cards"},
        configuration_version=5,
        proposed_pattern={"detection": {"pattern_name": None}},
    )
    rec = {"new_pattern_needed": True, "selected_endpoint": "/x/find/", "proposed_pattern": None}
    wf = _wf(w, recovery=rec, next_states=["approved", "unsupported", "archived"])
    assert _primary(wf) == "Retry automatic recovery"
    assert wf.current_step == "configure"
    assert wf.config_is_failed_historical
    assert "version 5" in wf.config_warning and "generic_html_cards" in wf.config_warning
    states = _states(wf)
    assert states["detect"] == "complete"
    assert states["configure"] == "current"
    assert states["preview"] == "blocked"
    assert states["approve"] == "blocked"
    assert states["run"] == "blocked"


def test_draft_with_no_preview_primary_is_test():
    w = _drafted("simpleview_events")
    wf = _wf(w, next_states=["approved", "archived"])
    assert _primary(wf) == "Test with preview"
    assert wf.current_step == "preview"


def test_draft_with_failed_preview_primary_is_fix():
    w = _drafted("simpleview_events")
    wf = _wf(w, preview=_preview(6, "failed", 0), next_states=["archived"])
    assert _primary(wf) == "Fix configuration"
    assert _states(wf)["preview"] == "failed"


def test_draft_with_stale_preview_primary_is_new_preview():
    w = _drafted("simpleview_events")
    wf = _wf(w, preview=_preview(5, "success", 4), next_states=["archived"])
    assert _primary(wf) == "Run a new preview"


def test_ready_for_approval_primary_is_approve():
    w = _drafted("simpleview_events")
    wf = _wf(w, preview=_preview(6, "success", 5), next_states=["approved", "archived"])
    assert _primary(wf) == "Approve configuration"
    assert wf.current_step == "approve"
    assert wf.approval_blockers == []


def test_approved_inactive_primary_is_activate():
    w = _website(
        onboarding_status="approved",
        approved_pattern={"pattern_name": "simpleview_events"},
        active_configuration_version=6,
        configuration={"pattern_name": "simpleview_events"},
        configuration_version=6,
    )
    wf = _wf(w, next_states=["active", "archived"])
    assert _primary(wf) == "Activate source"


def test_approved_active_primary_is_run():
    w = _website(
        onboarding_status="active",
        is_active=True,
        approved_pattern={"pattern_name": "x"},
        configuration={"pattern_name": "x"},
        last_success_at=datetime.now(UTC),
    )
    wf = _wf(w, next_states=["inactive", "failing", "archived"])
    assert _primary(wf) == "Run event import"
    assert wf.current_step == "run"


def test_failing_state_explains_and_offers_retry():
    w = _website(
        onboarding_status="failing",
        is_active=True,
        approved_pattern={"pattern_name": "x"},
        configuration={"pattern_name": "x"},
        consecutive_failure_count=3,
    )
    wf = _wf(w, next_states=["active", "inactive", "archived"])
    assert _primary(wf) == "Run event import"
    assert "3 consecutive" in wf.explanation


def test_archived_has_no_primary_workflow_action():
    w = _website(onboarding_status="archived", archived_at=datetime.now(UTC))
    wf = _wf(w, next_states=[])
    assert wf.primary_action is None
    assert wf.headline == "Archived"


# --- primary-action rules ---------------------------------------------------


def test_run_never_before_approval():
    w = _drafted("x")
    wf = _wf(w, preview=_preview(6, "success", 5), next_states=["approved"])
    assert _primary(wf) != "Run event import"


def test_approve_never_before_valid_preview():
    w = _drafted("x")
    wf = _wf(w, preview=_preview(6, "failed", 0), next_states=["archived"])
    assert _primary(wf) != "Approve configuration"
    assert wf.approval_blockers  # explained


def test_manual_pattern_selection_and_delete_never_primary_or_lifecycle():
    for status in ("unsupported", "needs_review", "draft", "approved", "active"):
        w = _website(
            onboarding_status=status,
            approved_pattern={"pattern_name": "x"} if status in ("approved", "active") else None,
            is_active=(status == "active"),
        )
        wf = _wf(w, next_states=["archived"])
        assert _primary(wf) not in ("Select pattern (draft only)", "Delete website…")
        assert all(a.label != "Delete website…" for a in wf.lifecycle_actions)


def test_delete_is_a_danger_action_only():
    wf = _wf(_website(), next_states=[])
    assert [a.label for a in wf.danger_actions] == ["Delete website…"]
    assert all(a.style == "danger" for a in wf.danger_actions)


def test_advanced_actions_present_for_privileged_user():
    wf = _wf(_website(onboarding_status="unsupported"), next_states=[])
    labels = [a.label for a in wf.advanced_actions]
    assert "Detect pattern only" in labels
    assert "Configure manually" in labels


def test_approval_blockers_hidden_when_no_permission():
    w = _drafted("x")
    wf = _wf(
        w,
        preview=_preview(6, "success", 5),
        perms={"update": True, "test": True, "approve": False, "delete": False},
    )
    assert any("permission to approve" in b for b in wf.approval_blockers)


# --- captured request recipe diagnostics ------------------------------------


def _recipe_dict():
    return {
        "method": "GET",
        "endpoint": "https://events.example.org/api/find/",
        "query_params": {
            "json": {"kind": "json_template", "value": {
                "filter": {"date_range": {"start": {"$date": {"kind": "window_start_utc"}}}},
                "options": {"skip": {"kind": "page_offset"}},
            }},
            "token": {"kind": "literal", "value": "PUB-TOKEN-XYZ-1234567890"},
        },
        "headers": {"Referer": {"kind": "source_page_url"}},
        "source_page_url": "https://events.example.org/events/",
        "pagination": {"kind": "offset", "limit": 100, "total_path": "docs.count"},
    }


def test_recipe_summary_surfaced_and_redacted():
    w = _drafted("simpleview_events")
    w.configuration = {"pattern_name": "simpleview_events", "request_recipe": _recipe_dict()}
    wf = _wf(w, preview=_preview(6, "success", 5))
    summary = wf.recipe_summary
    assert summary is not None
    assert summary["query_param_names"] == ["json", "token"]
    assert summary["pagination_kind"] == "offset"
    assert summary["dynamic_date_window"] is True
    assert summary["referer_present"] is True
    # Token present but never shown in the clear.
    assert summary["public_token_present"] is True
    assert "PUB-TOKEN-XYZ-1234567890" not in (summary["public_token_hint"] or "")


def test_recipe_summary_none_without_recipe():
    assert _wf(_drafted("simpleview_events")).recipe_summary is None


def test_recipe_summary_tolerates_malformed_recipe():
    w = _drafted("simpleview_events")
    w.configuration = {"pattern_name": "x", "request_recipe": {"nonsense": True}}
    # A malformed stored recipe must not raise — just no summary.
    assert _wf(w).recipe_summary is None


# --- rendering (route returns 200 for all major states) ---------------------


def _login_admin(client, make_super_admin, login, email):
    make_super_admin(email=email, password="root-pass-1234")
    login(email, "root-pass-1234")


def test_detail_renders_draft(client, make_super_admin, make_city, make_website, login, db_session):
    _login_admin(client, make_super_admin, login, "wf1@example.com")
    website = make_website(make_city(), name="Draft Site")
    resp = client.get(f"/admin/websites/{website.id}")
    assert resp.status_code == 200
    assert "What happens next?" in resp.text
    assert "Detect events automatically" in resp.text
    assert "Danger zone" in resp.text
    assert "Advanced tools" in resp.text


def test_detail_renders_website4_needs_review(
    client, make_super_admin, make_city, make_website, login, db_session
):
    from app.models.unsupported_site_report import UnsupportedSiteReport

    _login_admin(client, make_super_admin, login, "wf2@example.com")
    website = make_website(make_city(), name="Bloomington Weekend")
    website.onboarding_status = "needs_review"
    website.configuration = {"pattern_name": "generic_html_cards", "listing_url": "https://e/x"}
    website.configuration_version = 5
    website.proposed_pattern = {
        "detection": {
            "pattern_name": None,
            "confidence": 0.0,
            "discovered_endpoints": [],
            "warnings": [],
        }
    }
    db_session.add(website)
    db_session.add(
        UnsupportedSiteReport(
            website_id=website.id,
            submitted_url="https://e/x",
            fingerprint="fp",
            browser_recovery={
                "new_pattern_needed": True,
                "status": "structured_pattern_needed",
                "selected_endpoint": "/includes/rest_v2/.../find/",
                "proposed_pattern": None,
                "ignored_endpoint_count": 3,
            },
        )
    )
    db_session.commit()

    body = client.get(f"/admin/websites/{website.id}").text
    assert "Retry automatic recovery" in body
    # v5 clearly historical / not approvable.
    assert "failed historical" in body or "not eligible for approval" in body
    assert "version 5" in body
    # Must not present approval as an available action.
    assert "Approve configuration" not in body


def test_detail_renders_approved_active(
    client, make_super_admin, make_city, make_website, login, db_session
):
    _login_admin(client, make_super_admin, login, "wf3@example.com")
    website = make_website(
        make_city(),
        name="Active Site",
        approved_pattern={"pattern_name": "simpleview_events", "listing_url": "https://e"},
        active_configuration_version=6,
    )
    website.onboarding_status = "active"
    website.is_active = True
    db_session.add(website)
    db_session.commit()

    body = client.get(f"/admin/websites/{website.id}").text
    assert "Run event import" in body
    assert "Event import status" in body


def test_detail_renders_archived(
    client, make_super_admin, make_city, make_website, login, db_session
):
    _login_admin(client, make_super_admin, login, "wf4@example.com")
    website = make_website(make_city(), name="Archived Site", archived=True)
    website.onboarding_status = "archived"
    db_session.add(website)
    db_session.commit()

    body = client.get(f"/admin/websites/{website.id}").text
    assert "Archived" in body
    assert "No workflow action is available" in body


def test_detail_renders_with_sparse_data(
    client, make_super_admin, make_city, make_website, login, db_session
):
    # No proposed_pattern, no preview, no recovery, no runs — must not error.
    _login_admin(client, make_super_admin, login, "wf5@example.com")
    website = make_website(make_city(), name="Sparse Site")
    website.onboarding_status = "unsupported"
    db_session.add(website)
    db_session.commit()
    assert client.get(f"/admin/websites/{website.id}").status_code == 200


def test_detail_hides_privileged_controls_for_readonly_user(
    client, make_user, make_city, make_website, login, db_session
):
    from app.core.permissions import EDITOR

    make_user(email="wf-ro@example.com", password="ro-pass-1234", role_name=EDITOR)
    website = make_website(make_city(), name="RO Site")
    login("wf-ro@example.com", "ro-pass-1234")
    resp = client.get(f"/admin/websites/{website.id}")
    assert resp.status_code == 200
    # An editor without delete permission sees no Danger zone.
    assert "Danger zone" not in resp.text


# --- context wiring + preview-backed states ---------------------------------


def _add_preview_run(db, website, *, version, status, valid, found=None, rejected=0):
    from app.models.extraction_run import ExtractionRun

    run = ExtractionRun(
        website_id=website.id, configuration_version=version, pattern_name="simpleview_events",
        run_type="preview", status=status, source_url="https://e/x",
        events_found=found if found is not None else valid, events_valid=valid,
        events_rejected=rejected, started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    return run


def _drafted_website(db, make_city, make_website, name, *, version=6, pattern="simpleview_events"):
    website = make_website(make_city(), name=name)
    website.onboarding_status = "detected"
    website.configuration = {"pattern_name": pattern, "api_endpoint": "https://e/x"}
    website.configuration_version = version
    website.proposed_pattern = {"detection": {"pattern_name": pattern}}
    db.add(website)
    db.commit()
    return website


def test_workflow_is_in_render_context_as_view_model(
    client, make_super_admin, make_city, make_website, login, db_session, monkeypatch
):
    """The exact guard against the reported 500: the detail render must always
    receive a WebsiteWorkflow under the key 'workflow'."""
    import app.routers.websites as mod
    from app.services.website_workflow import WebsiteWorkflow

    _login_admin(client, make_super_admin, login, "wf-ctx@example.com")
    website = make_website(make_city(), name="Ctx Site")

    captured: dict = {}
    real_render = mod.render

    def _spy(request, template_name, context=None, **kw):
        if template_name == "admin/websites/detail.html":
            captured["context"] = context
        return real_render(request, template_name, context, **kw)

    monkeypatch.setattr(mod, "render", _spy)
    assert client.get(f"/admin/websites/{website.id}").status_code == 200
    assert "workflow" in captured["context"]
    assert isinstance(captured["context"]["workflow"], WebsiteWorkflow)
    # Exactly one primary action object (never a collection).
    from app.services.website_workflow import WorkflowAction

    primary = captured["context"]["workflow"].primary_action
    assert primary is None or isinstance(primary, WorkflowAction)


def test_detail_renders_unsupported(
    client, make_super_admin, make_city, make_website, login, db_session
):
    _login_admin(client, make_super_admin, login, "wf-uns@example.com")
    website = make_website(make_city(), name="Unsupported Site")
    website.onboarding_status = "unsupported"
    website.proposed_pattern = {"detection": {"pattern_name": None}}
    db_session.add(website)
    db_session.commit()
    assert client.get(f"/admin/websites/{website.id}").status_code == 200


def test_detail_renders_failed_preview(
    client, make_super_admin, make_city, make_website, login, db_session
):
    _login_admin(client, make_super_admin, login, "wf-fp@example.com")
    website = _drafted_website(db_session, make_city, make_website, "Failed Preview Site")
    _add_preview_run(db_session, website, version=6, status="failed", valid=0)
    body = client.get(f"/admin/websites/{website.id}").text
    assert "Fix configuration" in body
    assert "Approve configuration" not in body


def test_detail_renders_ready_for_approval(
    client, make_super_admin, make_city, make_website, login, db_session
):
    _login_admin(client, make_super_admin, login, "wf-ready@example.com")
    website = _drafted_website(db_session, make_city, make_website, "Ready Site")
    _add_preview_run(db_session, website, version=6, status="success", valid=5, found=6, rejected=1)
    body = client.get(f"/admin/websites/{website.id}").text
    assert body.count("Approve configuration") >= 1


def test_detail_renders_approved_inactive(
    client, make_super_admin, make_city, make_website, login, db_session
):
    _login_admin(client, make_super_admin, login, "wf-ai@example.com")
    website = make_website(
        make_city(), name="Approved Inactive",
        approved_pattern={"pattern_name": "simpleview_events", "api_endpoint": "https://e"},
        active_configuration_version=6,
    )
    website.onboarding_status = "approved"
    db_session.add(website)
    db_session.commit()
    body = client.get(f"/admin/websites/{website.id}").text
    assert "Activate source" in body


def test_detail_renders_needs_review_without_recovery_evidence(
    client, make_super_admin, make_city, make_website, login, db_session
):
    # needs_review with NO browser-recovery evidence and no draft — must not crash.
    _login_admin(client, make_super_admin, login, "wf-nr@example.com")
    website = make_website(make_city(), name="No Recovery Site")
    website.onboarding_status = "needs_review"
    website.proposed_pattern = {"detection": {"pattern_name": None}}
    db_session.add(website)
    db_session.commit()
    assert client.get(f"/admin/websites/{website.id}").status_code == 200


def test_detail_renders_preview_without_quality_data(
    client, make_super_admin, make_city, make_website, login, db_session
):
    # A draft with a preview run but NO inference/quality summary must not crash.
    _login_admin(client, make_super_admin, login, "wf-nq@example.com")
    website = _drafted_website(db_session, make_city, make_website, "No Quality Site")
    _add_preview_run(db_session, website, version=6, status="success", valid=3)
    assert client.get(f"/admin/websites/{website.id}").status_code == 200


# --- polish: single primary, grouped errors, lifecycle, relabel -------------


def test_view_model_groups_repeated_preview_errors():
    from types import SimpleNamespace as _NS

    preview = _NS(
        configuration_version=6, status="failed", events_valid=0, events_found=5,
        events_rejected=5,
        error_summary=(
            "candidate[0]: a parseable start date is required; "
            "candidate[1]: a parseable start date is required; "
            "candidate[2]: a parseable start date is required; "
            "candidate[3]: a parseable start date is required; "
            "candidate[4]: canonical URL is required"
        ),
    )
    wf = _wf(_drafted("simpleview_events"), preview=preview, next_states=["archived"])
    assert wf.preview_error_groups[0] == ("a parseable start date is required", 4)
    assert ("canonical URL is required", 1) in wf.preview_error_groups
    # Raw errors are preserved for the technical view, not discarded.
    assert len(wf.preview_raw_errors) == 5


def test_move_to_approved_hidden_when_not_approvable():
    # needs_review with a failed historical draft: approval is blocked, so the
    # 'approved' lifecycle jump is filtered out and explained.
    w = _website(
        onboarding_status="needs_review",
        configuration={"pattern_name": "generic_html_cards"},
        configuration_version=5,
        proposed_pattern={"detection": {"pattern_name": None}},
    )
    rec = {"new_pattern_needed": True, "selected_endpoint": "/x/find/", "proposed_pattern": None}
    wf = _wf(w, recovery=rec, next_states=["approved", "unsupported", "archived"])
    labels = [a.label for a in wf.lifecycle_actions]
    assert not any("approved" in label.lower() for label in labels)
    assert any("not approvable" in note for note in wf.lifecycle_notes)


def test_historical_config_secondary_relabelled():
    w = _website(
        onboarding_status="needs_review",
        configuration={"pattern_name": "generic_html_cards"},
        configuration_version=5,
        proposed_pattern={"detection": {"pattern_name": None}},
    )
    rec = {"new_pattern_needed": True, "proposed_pattern": None}
    wf = _wf(w, recovery=rec, next_states=["archived"])
    labels = [a.label for a in wf.secondary_actions]
    assert "Inspect failed configuration" in labels
    assert "Review configuration" not in labels


def test_detail_renders_exactly_one_primary_styled_action(
    client, make_super_admin, make_city, make_website, login, db_session
):
    from app.models.unsupported_site_report import UnsupportedSiteReport

    _login_admin(client, make_super_admin, login, "wf-1p@example.com")
    website = make_website(make_city(), name="One Primary Site")
    website.onboarding_status = "needs_review"
    website.configuration = {"pattern_name": "generic_html_cards", "listing_url": "https://e/x"}
    website.configuration_version = 5
    website.proposed_pattern = {"detection": {"pattern_name": None}}
    db_session.add(website)
    db_session.add(
        UnsupportedSiteReport(
            website_id=website.id, submitted_url="https://e/x", fingerprint="fp",
            browser_recovery={"new_pattern_needed": True, "status": "structured_pattern_needed",
                              "selected_endpoint": "/x/find/", "proposed_pattern": None},
        )
    )
    db_session.commit()

    body = client.get(f"/admin/websites/{website.id}").text
    # Exactly one primary-styled action control on the whole page.
    assert body.count("wf-primary") == 1
    # The retry action itself appears exactly twice: the header primary and the
    # one contextual button in the recovery card (prose mentions don't count).
    assert body.count(f"/admin/websites/{website.id}/browser-retry") == 2
    # 'Move to approved' contradiction removed; explanation shown.
    assert "Lifecycle approval is unavailable" in body
    # Historical action relabelled.
    assert "Inspect failed configuration" in body


def test_detail_renders_grouped_preview_errors(
    client, make_super_admin, make_city, make_website, login, db_session
):
    _login_admin(client, make_super_admin, login, "wf-ge@example.com")
    website = _drafted_website(db_session, make_city, make_website, "Grouped Errors Site")
    run = _add_preview_run(db_session, website, version=6, status="failed", valid=0, found=5)
    run.error_summary = (
        "candidate[0]: a parseable start date is required; "
        "candidate[1]: a parseable start date is required; "
        "candidate[2]: canonical URL is required"
    )
    db_session.commit()

    body = client.get(f"/admin/websites/{website.id}").text
    assert "2 candidates — a parseable start date is required." in body
    assert "1 candidate — canonical URL is required." in body
    assert "View technical validation details" in body


def test_detail_recovery_not_attempted_message(
    client, make_super_admin, make_city, make_website, login, db_session
):
    _login_admin(client, make_super_admin, login, "wf-na@example.com")
    website = make_website(make_city(), name="No Recovery Yet")
    website.onboarding_status = "needs_review"
    website.proposed_pattern = {"detection": {"pattern_name": None}}
    db_session.add(website)
    db_session.commit()
    body = client.get(f"/admin/websites/{website.id}").text
    assert "Automatic browser recovery has not been attempted yet." in body


# --- approved lifecycle action must never render when not approvable --------


def _failed_historical():
    """The observed Bloomington state: needs_review + inactive + a failed,
    historical simpleview_events v6 draft whose matching preview is blocked."""
    w = _website(
        onboarding_status="needs_review",
        configuration={"pattern_name": "simpleview_events", "api_endpoint": "https://x/find/"},
        configuration_version=6,
        proposed_pattern={"detection": {"pattern_name": "simpleview_events"}},
    )
    preview = _preview(6, "blocked", 0)
    recovery = {"new_pattern_needed": False, "proposed_pattern": "simpleview_events",
                "preview_status": "blocked"}
    wf = _wf(w, preview=preview, recovery=recovery,
             next_states=["approved", "unsupported", "archived"])
    return wf


def test_view_model_excludes_approved_when_not_approvable():
    wf = _failed_historical()
    assert wf.approval_allowed is False
    assert wf.config_is_failed_historical is True
    targets = [a.status_target for a in wf.lifecycle_actions]
    assert "approved" not in targets
    assert not any("approved" in a.label.lower() for a in wf.lifecycle_actions)
    assert any("not approvable" in note for note in wf.lifecycle_notes)
    # Non-approval transitions remain available.
    assert "unsupported" in targets
    assert "archived" in targets


def test_view_model_includes_fast_track_when_approvable():
    wf = _wf(_drafted("simpleview_events"), preview=_preview(6, "success", 5),
             next_states=["approved", "archived"])
    assert wf.approval_allowed is True
    labels = [a.label for a in wf.lifecycle_actions]
    assert "Fast-track lifecycle to approved" in labels
    # Still a secondary style; the guided Approve action stays primary.
    approved_action = next(a for a in wf.lifecycle_actions if a.status_target == "approved")
    assert approved_action.style == "secondary"


def _failed_historical_website(db, make_city, make_website, name):
    website = make_website(make_city(), name=name)
    website.onboarding_status = "needs_review"
    website.configuration = {"pattern_name": "simpleview_events", "api_endpoint": "https://x/find/"}
    website.configuration_version = 6
    website.proposed_pattern = {"detection": {"pattern_name": "simpleview_events"}}
    db.add(website)
    db.commit()
    _add_preview_run(db, website, version=6, status="blocked", valid=0)
    return website


def test_detail_hides_approved_lifecycle_for_failed_historical(
    client, make_super_admin, make_city, make_website, login, db_session
):
    _login_admin(client, make_super_admin, login, "wf-la@example.com")
    website = _failed_historical_website(db_session, make_city, make_website, "Bloomington Fail")
    body = client.get(f"/admin/websites/{website.id}").text
    assert "Move to approved" not in body
    assert "Fast-track lifecycle to approved" not in body
    assert "Lifecycle approval is unavailable while the current configuration" in body
    # Other lifecycle transitions remain.
    assert "Move to unsupported" in body
    assert "Archive" in body


def test_template_defense_drops_injected_approved_action(
    client, make_super_admin, make_city, make_website, login, db_session, monkeypatch
):
    """Even if the view model is malformed (contains an approved action while
    approval is not allowed), the template must not render it."""
    import app.routers.websites as mod
    from app.services.website_workflow import WorkflowAction
    from app.services.website_workflow import build_website_workflow as real_builder

    _login_admin(client, make_super_admin, login, "wf-inj@example.com")
    website = make_website(make_city(), name="Injected Site")
    website.onboarding_status = "needs_review"
    db_session.add(website)
    db_session.commit()

    def _malformed(**kwargs):
        wf = real_builder(**kwargs)
        wf.approval_allowed = False
        wf.lifecycle_actions = [
            *wf.lifecycle_actions,
            WorkflowAction(
                label="Move to approved", url=f"/admin/websites/{website.id}/status",
                style="secondary", status_target="approved",
            ),
        ]
        return wf

    monkeypatch.setattr(mod, "build_website_workflow", _malformed)
    body = client.get(f"/admin/websites/{website.id}").text
    assert "Move to approved" not in body
    assert 'value="approved"' not in body


def test_bloomington_recovery_renders_single_primary_retry(
    client, make_super_admin, make_city, make_website, login, db_session
):
    _login_admin(client, make_super_admin, login, "wf-blm@example.com")
    website = _failed_historical_website(db_session, make_city, make_website, "Bloomington Rec")
    body = client.get(f"/admin/websites/{website.id}").text
    assert body.count("wf-primary") == 1
    assert body.count(f"/admin/websites/{website.id}/browser-retry") == 2  # header + recovery card


def test_recovery_draft_and_preview_status_are_separate_rows(
    client, make_super_admin, make_city, make_website, login, db_session
):
    from app.models.unsupported_site_report import UnsupportedSiteReport

    _login_admin(client, make_super_admin, login, "wf-ws@example.com")
    website = make_website(make_city(), name="Wording Site")
    website.onboarding_status = "needs_review"
    website.proposed_pattern = {"detection": {"pattern_name": "simpleview_events"}}
    db_session.add(website)
    db_session.add(
        UnsupportedSiteReport(
            website_id=website.id, submitted_url="https://x/find/", fingerprint="fp",
            browser_recovery={"status": "blocked", "proposed_pattern": "simpleview_events",
                              "preview_status": "blocked", "blocked_reason": "http_403"},
        )
    )
    db_session.commit()
    body = client.get(f"/admin/websites/{website.id}").text
    assert "Draft created" in body
    assert "Preview status" in body
    assert "Draft created / preview run" not in body
    # 'blocked' is presented as a preview status, capitalized, not a yes/no.
    assert "Blocked" in body
