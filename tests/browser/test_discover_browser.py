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
    heading = page.get_by_role("heading", name="Snoozed & dismissed")
    assert heading.is_visible()
    heading.click()                                 # collapsed by default — expand to see details
    tag = page.get_by_text("dismissed", exact=True)   # the tag, not the heading
    tag.wait_for(state="visible", timeout=2000)       # auto-wait for x-show to apply
    assert page.get_by_text("Old Mix").is_visible()


def test_snoozed_section_collapsed_by_default(live_app, page):
    page.goto(f"{live_app}/discover")
    page.get_by_role("row").filter(has_text="Old Mix").get_by_role(
        "button", name="Dismiss").click()
    page.get_by_text("Old Mix").first.wait_for(state="hidden", timeout=3000)
    page.goto(f"{live_app}/discover")
    heading = page.get_by_role("heading", name="Snoozed & dismissed")
    assert heading.is_visible()
    restore = page.get_by_role("button", name="Restore")
    assert not restore.is_visible()                 # details collapsed on load
    heading.click()
    restore.wait_for(state="visible", timeout=2000)  # expands on click


def test_dismiss_error_shows_toast(live_app, page):
    page.goto(f"{live_app}/discover")
    # drive a malformed request through htmx from the page context; expect a toast, no crash
    page.evaluate("htmx.ajax('POST', '/rediscover/dismiss', {values: {}, target: 'body', swap: 'none'})")
    toast = page.locator("#toasts .toast-err")
    toast.wait_for(state="visible", timeout=3000)
    assert "Nothing to dismiss" in toast.inner_text()
    toast.wait_for(state="hidden", timeout=7000)        # Alpine auto-dismiss (proves re-init on swap)
