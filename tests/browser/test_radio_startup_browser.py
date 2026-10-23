"""An old Radio checkpoint must not hide the launcher after restarting the app."""
from concurrent.futures import ThreadPoolExecutor

from playwright.sync_api import expect

from tests.test_radio_session import poc, play


def test_restart_with_old_radio_keeps_the_seed_grid_visible(poc, store, page, live_app):
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    c, _, frames, *_ = poc
    c.start()
    # Playwright owns an event loop on this thread. The synchronous controller
    # drives its async bridge on its normal worker thread.
    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(play, c, "v1").result(timeout=5)
    c.suspend()
    c.seeds()
    # The new app has this checkpoint but no confirmed Radio playback. Its real
    # bootstrap and status endpoints must agree, including during heartbeat gaps.
    page.goto(f"{live_app}/#radio")
    seeds = page.locator(".radio-seeds")
    launch = page.get_by_role("status", name="Starting radio")
    expect(seeds).to_be_visible()
    expect(page.locator(".radio-seed")).to_have_count(16)
    expect(launch).to_be_hidden()
    for _ in range(3):
        page.evaluate("async () => { await Alpine.$data(document.querySelector('.radio-home')).refresh(); }")
        expect(seeds).to_be_visible()
        expect(launch).to_be_hidden()
    assert len(frames) == 1
