from app.extraction.detection import run_detection
from app.extraction.unsupported import build_report, report_fingerprint
from tests.extraction_helpers import make_response, make_response_from_fixture


def test_unsupported_page_report_has_expected_evidence():
    response = make_response_from_fixture("unsupported_page.html")
    detection = run_detection(response)
    report = build_report(
        website_id=1,
        submitted_url="https://example.com/events",
        response=response,
        detection=detection,
        failure_reason="no_pattern_matched",
    )
    assert report.website_id == 1
    assert report.page_title == "Just a regular page"
    assert report.json_ld_presence is False
    assert report.access_denied_or_challenge_detected is False
    assert report.failure_reason == "no_pattern_matched"


def test_blocked_report_marks_access_denied():
    response = make_response("blocked", blocked_reason="ssrf_blocked:example")
    detection = run_detection(response)
    report = build_report(
        website_id=1,
        submitted_url="https://example.com/events",
        response=response,
        detection=detection,
        failure_reason=response.blocked_reason,
    )
    assert report.access_denied_or_challenge_detected is True


def test_browser_required_indication_surfaced():
    html = "<html><body><script>" + "x = 1;" * 500 + "</script><div id='app'></div></body></html>"
    response = make_response(html)
    detection = run_detection(response)
    report = build_report(
        website_id=1,
        submitted_url="https://example.com/events",
        response=response,
        detection=detection,
        failure_reason="no_pattern_matched",
    )
    assert report.browser_required is True


def test_report_fingerprint_is_stable_for_identical_inputs():
    response = make_response_from_fixture("unsupported_page.html")
    detection = run_detection(response)
    fp1 = report_fingerprint(1, detection, response.status_code, "no_pattern_matched")
    fp2 = report_fingerprint(1, detection, response.status_code, "no_pattern_matched")
    assert fp1 == fp2


def test_report_fingerprint_differs_for_different_websites():
    response = make_response_from_fixture("unsupported_page.html")
    detection = run_detection(response)
    fp1 = report_fingerprint(1, detection, response.status_code, "no_pattern_matched")
    fp2 = report_fingerprint(2, detection, response.status_code, "no_pattern_matched")
    assert fp1 != fp2
