"""Enrichment counts navigate to useful details and update through the real HTMX poll."""
from playwright.sync_api import expect
import pytest


def _seed(store):
    first = store.tracks.upsert_track("v1", "Checked song", "First artist", None, 200)
    second = store.tracks.upsert_track("v2", "Queued song", "Second artist", None, 200)
    store.tracks.set_track_enrichment(first, "Rock", "1999")
    store.enrichment_queue.mark_enriched([first], 1.0)
    store.enrichment.upsert_conflict(first, "genre", [{"provider": "musicbrainz", "value": "Rock"},
                                           {"provider": "discogs", "value": "Pop"}])
    return first, second


def test_tiles_navigate_and_conflict_choice_is_saved(live_app, store, page):
    first, _ = _seed(store)
    page.goto(f"{live_app}/enrich")
    status = page.locator(".enrich-status-card")
    coverage = page.locator(".enrich-coverage-card")
    expect(status.get_by_role("heading", name="Status", exact=True)).to_be_visible()
    expect(status.locator(".metric")).to_have_count(0)
    expect(coverage.locator(".enrich-activity")).to_have_count(0)
    expect(page.locator('.metric[href="/enrich/processed"] .v')).to_have_text("50%")
    page.locator('.metric[href="/enrich/queue"]').click()
    expect(page.get_by_role("heading", name="Enrichment queue")).to_be_visible()
    expect(page.get_by_role("cell", name="Queued song", exact=True)).to_be_visible()
    page.goto(f"{live_app}/enrich")
    page.locator('.metric[href="/enrich/processed"]').click()
    expect(page.get_by_role("cell", name="Checked song", exact=True)).to_be_visible()
    page.goto(f"{live_app}/enrich")
    page.locator('.metric[href="/enrich/conflicts"]').click()
    expect(page.get_by_role("radio", name="Rock", exact=True)).to_be_checked()
    expect(page.locator(".conflict-choice.is-current")).to_contain_text("MusicBrainz")
    expect(page.locator(".conflict-help-icon svg")).to_be_visible()
    expect(page.get_by_role("button", name="Keep selected value")).to_have_count(0)
    page.get_by_role("radio", name="Pop", exact=True).check()
    expect(page.get_by_role("status")).to_have_text("Saved")
    expect(page.locator(".conflict-save-status svg")).to_be_visible()
    expect(page.locator(".conflict-choice.is-current")).to_contain_text("Pop")
    expect(page.get_by_role("radio", name="Pop", exact=True)).to_be_checked()
    assert store.tracks.get_track_enrichment(first)[0] == "Pop"
    # The card stays in place; another click can correct a choice without navigating away.
    page.get_by_role("radio", name="Rock", exact=True).check()
    expect(page.get_by_role("status")).to_have_text("Saved")
    expect(page.locator(".conflict-choice.is-current")).to_contain_text("Rock")
    assert store.tracks.get_track_enrichment(first)[0] == "Rock"
    page.reload()
    expect(page.get_by_text("No open conflicts.", exact=True)).to_be_visible()


def test_counts_advance_without_reloading_and_fit_mobile(live_app, store, page):
    first, second = _seed(store)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{live_app}/enrich")
    status = page.locator(".enrich-status-card")
    expect(page.locator('.metric[href="/enrich/queue"] .v')).to_have_text("1")
    store.enrichment_queue.mark_enriched([second], 1.0)
    expect(page.locator('.metric[href="/enrich/queue"] .v')).to_have_text("0", timeout=6000)
    expect(page.locator('.metric[href="/enrich/processed"] .v')).to_have_text("100%")
    expect(page.get_by_text("Caught up", exact=False)).to_be_visible()
    expect(status.get_by_text("Caught up", exact=True)).to_be_visible()
    status.get_by_role("button", name="Pause worker").click()
    expect(status.get_by_text("Paused", exact=True)).to_be_visible()
    status.get_by_role("button", name="Resume worker").click()
    expect(status.get_by_text("Caught up", exact=True)).to_be_visible()
    configuration = {"musicbrainz": {"fingerprint": "old", "status": "available"}}
    store.enrichment_state.reconcile_provider_configuration(configuration)
    store.enrichment_state.reconcile_provider_configuration({
        "musicbrainz": {"fingerprint": "new", "status": "available"}})
    queue = page.locator('.metric[href="/enrich/queue"]')
    expect(queue.locator(".v")).to_have_text("2", timeout=6000)
    expect(queue).to_contain_text("0 awaiting first enrichment")
    expect(queue).to_contain_text("2 queued for re-enrichment")
    queue.click()
    expect(page.get_by_role("cell", name="Re-enrichment", exact=True)).to_have_count(2)
    track = store.tracks.catalog_subject(first)
    store.enrichment_state.record_enrichment_attempt(track["identity_key"], "musicbrainz", {}, now=10.0)
    store.enrichment_queue.mark_enriched([first], 10.0)
    page.goto(f"{live_app}/enrich")
    expect(page.locator('.metric[href="/enrich/queue"] .v')).to_have_text("1")
    expect(page.locator('.metric[href="/enrich/processed"] .v')).to_have_text("50%")
    expect(page.locator(".spark-refresh")).to_be_visible()
    expect(page.locator(".enrich-chart-legend")).to_contain_text("Latest re-enrichment")
    assert page.locator(".spark rect").last.evaluate(
        "el => JSON.parse(el.dataset.tip).rows.map(row => row.v)") == [2, 1]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [1280, 390])
def test_one_queue_shows_library_and_discovery_tracks(live_app, store, page, width, tmp_path):
    store.tracks.upsert_track("lib", "Library song", "Artist", None, 200)
    store.discovery.upsert_discovered_track("new|artist", "new", "Discovery song", "Artist", None, None,
                                  None, None, "radio:seed", 1.0)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{live_app}/enrich")
    expect(page.locator('.metric[href="/enrich/queue"] .v')).to_have_text("2")
    expect(page.get_by_text("0 of 2 tracks checked", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=tmp_path / f"enrichment-{width}.png", full_page=True)
    page.locator('.metric[href="/enrich/queue"]').click()
    expect(page.get_by_role("cell", name="Discovery song", exact=True)).to_be_visible()
    expect(page.get_by_role("cell", name="Library song", exact=True)).to_be_visible()
    expect(page.get_by_role("navigation", name="Enrichment queues")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [1280, 390])
def test_detail_headings_stay_on_the_left(live_app, store, page, width):
    _seed(store)
    page.set_viewport_size({"width": width, "height": 900})
    for view in ("queue", "processed", "conflicts"):
        page.goto(f"{live_app}/enrich/{view}")
        head = page.locator(".page-head").bounding_box()
        heading = page.get_by_role("heading", level=1).bounding_box()
        assert abs(heading["x"] - head["x"]) < 2
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_clicking_current_radio_saves_and_keyboard_choice_updates(live_app, store, page):
    first, _ = _seed(store)
    page.goto(f"{live_app}/enrich/conflicts")
    # Clicking the already-checked current value must still count as an explicit choice.
    page.get_by_role("radio", name="Rock", exact=True).click()
    expect(page.get_by_role("status")).to_have_text("Saved")
    assert store.enrichment_reports.outstanding_conflicts() == 0
    page.get_by_role("radio", name="Rock", exact=True).focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator(".conflict-choice.is-current")).to_contain_text("Pop")
    expect(page.get_by_role("status")).to_have_text("Saved")
    expect(page.get_by_role("radio", name="Pop", exact=True)).to_be_focused()
    assert store.tracks.get_track_enrichment(first)[0] == "Pop"


def test_failed_save_restores_current_radio_and_retry_works(live_app, store, page):
    first, _ = _seed(store)
    page.goto(f"{live_app}/enrich/conflicts")
    page.route("**/enrich/conflicts/*/genre*", lambda route: route.fulfill(status=500, body="Failed"))
    page.get_by_role("radio", name="Pop", exact=True).check()
    expect(page.get_by_role("status")).to_contain_text("Could not save")
    expect(page.get_by_role("radio", name="Rock", exact=True)).to_be_checked()
    assert store.tracks.get_track_enrichment(first)[0] == "Rock"
    page.unroute("**/enrich/conflicts/*/genre*")
    page.get_by_role("radio", name="Pop", exact=True).check()
    expect(page.get_by_role("status")).to_have_text("Saved")
    assert store.tracks.get_track_enrichment(first)[0] == "Pop"
