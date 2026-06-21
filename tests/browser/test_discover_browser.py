import pytest

pytestmark = pytest.mark.browser


def test_harness_loads_discover(live_app, page):
    page.goto(f"{live_app}/discover")
    assert page.get_by_role("heading", name="Stale & low-listen").is_visible()
    assert page.get_by_text("Old Mix").is_visible()


def test_dismiss_removes_candidate(live_app, page):
    page.goto(f"{live_app}/discover")
    row = page.get_by_role("row").filter(has_text="Old Mix")
    assert row.is_visible()
    row.get_by_role("button", name="Dismiss").click()
    # the candidate row leaves the stale list (fade then gone)
    page.get_by_text("Old Mix").first.wait_for(state="hidden", timeout=3000)


def test_snooze_removes_candidate(live_app, page):
    page.goto(f"{live_app}/discover")
    page.get_by_role("row").filter(has_text="Old Mix").get_by_role(
        "button", name="Snooze 30d").click()
    page.get_by_text("Old Mix").first.wait_for(state="hidden", timeout=3000)


def test_dismissed_appears_in_snoozed_after_reload(live_app, page):
    page.goto(f"{live_app}/discover")
    page.get_by_role("row").filter(has_text="Old Mix").get_by_role(
        "button", name="Dismiss").click()
    page.get_by_text("Old Mix").first.wait_for(state="hidden", timeout=3000)
    page.goto(f"{live_app}/discover")               # reload: now under "Snoozed & dismissed"
    assert page.get_by_role("heading", name="Snoozed & dismissed").is_visible()
    assert page.get_by_text("dismissed", exact=True).is_visible()   # the tag, not the heading
    assert page.get_by_text("Old Mix").is_visible()


def test_dismiss_error_shows_toast(live_app, page):
    page.goto(f"{live_app}/discover")
    # drive a malformed request through htmx from the page context; expect a toast, no crash
    page.evaluate("htmx.ajax('POST', '/rediscover/dismiss', {values: {}, target: 'body', swap: 'none'})")
    toast = page.locator("#toasts .toast-err")
    toast.wait_for(state="visible", timeout=3000)
    assert "Nothing to dismiss" in toast.inner_text()
    toast.wait_for(state="hidden", timeout=7000)        # Alpine auto-dismiss (proves re-init on swap)
