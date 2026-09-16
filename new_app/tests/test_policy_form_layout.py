"""The policy form is 60 controls; it must not render as one tall column.

Before this it was 4,382px of stacked full-width inputs with labels generated
by field.replace("_", " "), so an administrator read "minimum start date parse
success" and scrolled five and a half screens to reach Save.
"""

import re

import pytest

from app.core.permissions import SUPER_ADMINISTRATOR
from app.routers.auto_onboarding_policies import (
    COLLAPSED_SECTIONS,
    FIELD_LABELS,
    FORM_SECTIONS,
)


@pytest.fixture
def form_html(client, make_super_admin, login):
    make_super_admin(email="policy-form@example.com", password="policy-pass-123")
    login("policy-form@example.com", "policy-pass-123")
    response = client.get("/admin/settings/onboarding-policies/new")
    assert response.status_code == 200
    return response.text


def test_every_section_field_has_a_written_label():
    """A label map that drifts from the sections silently falls back to the
    underscored column name, which is what this replaced."""
    fields = {f for _, fields in FORM_SECTIONS for f in fields}
    assert fields - set(FIELD_LABELS) == set()
    assert set(FIELD_LABELS) - fields == set()


def test_no_label_is_just_the_underscored_column_name():
    for field, label in FIELD_LABELS.items():
        assert label != field.replace("_", " "), field
        assert "_" not in label, field


def test_collapsed_sections_name_real_sections():
    titles = {title for title, _ in FORM_SECTIONS}
    assert COLLAPSED_SECTIONS <= titles


def test_form_renders_written_labels_not_column_names(form_html):
    assert "Activate automatically (goes public)" in form_html
    assert "Minimum start dates that parse" in form_html
    assert "minimum start date parse success" not in form_html


def test_fields_are_laid_out_in_grids(form_html):
    assert 'class="field-grid"' in form_html
    assert 'class="checkbox-grid"' in form_html


def test_rarely_used_sections_start_collapsed(form_html):
    """<details open> for the everyday sections, plain <details> for the rest."""
    sections = re.findall(r'<details class="policy-section"([^>]*)>', form_html)
    assert len(sections) == len(FORM_SECTIONS)
    collapsed = [s for s in sections if "open" not in s]
    assert len(collapsed) == len(COLLAPSED_SECTIONS)


def test_confirmations_are_not_styled_as_unavailable(form_html):
    """.placeholder-note means "not yet available" elsewhere in the admin; these
    two controls are live and are the riskiest on the page."""
    assert form_html.count('class="confirm-callout"') == 2
    assert "ENABLE AUTO APPROVAL" in form_html
    assert "ENABLE AUTO ACTIVATION" in form_html


def test_policy_form_opts_out_of_the_narrow_shared_form_width(form_html):
    assert 'class="admin-form policy-form"' in form_html


@pytest.fixture
def detail_html(client, make_super_admin, login, db_session):
    from app.models.auto_onboarding_policy import AutoOnboardingPolicy

    make_super_admin(email="policy-detail@example.com", password="policy-pass-123")
    login("policy-detail@example.com", "policy-pass-123")
    policy = db_session.query(AutoOnboardingPolicy).first()
    response = client.get(f"/admin/settings/onboarding-policies/{policy.id}")
    assert response.status_code == 200
    return response.text


def test_detail_shows_every_setting_not_a_chosen_ten(detail_html):
    """It used to render a 13-row table covering 10 of the 44 settings, so a
    policy's thresholds were invisible until you opened the edit form."""
    for _, fields in FORM_SECTIONS:
        for field in fields:
            assert FIELD_LABELS[field] in detail_html, field


def test_detail_mirrors_the_form_sections(detail_html):
    for title, _ in FORM_SECTIONS:
        assert f"<h2>{title}</h2>" in detail_html
    assert detail_html.count('class="policy-section"') == len(FORM_SECTIONS)


def test_detail_uses_badges_for_on_off_not_prose(detail_html):
    assert "<td>Enabled</td>" not in detail_html
    assert "<td>Disabled</td>" not in detail_html
    assert 'class="badge badge-muted">Off<' in detail_html


def test_detail_page_actions_precede_the_settings(detail_html):
    """Edit/Deactivate are page-level actions; stranded after the collapsed
    sections they read as belonging to the last section."""
    assert detail_html.index('class="form-actions"') < detail_html.index('class="policy-section"')
