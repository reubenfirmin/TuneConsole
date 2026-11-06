"""Exercise the recap against a packaged server with the real idle-shutdown monitor."""
from contextlib import contextmanager
import multiprocessing
import os
from pathlib import Path
import socket
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.test_trends_recap_route import _recap_app, _STORY, _detailed_story
from tests.process_coverage import ChildCoverage, SpawnedCoveragePhases, run_spawned, switch_spawned_phase
from yt_playlist.core.store import Store


def _serve_recap(port, data_dir, ready):
    os.environ["YT_PLAYLIST_EXIT_ON_IDLE"] = "1"
    os.environ["YT_PLAYLIST_HOME"] = data_dir
    # Keep production startup protection until a real document has a lease.
    # Then expire only that startup deadline; the real close grace and monitor
    # continue to govern navigation, downloads and multiple browser documents.
    from yt_playlist.web import app as web_app
    class DocumentReadyPresence(web_app._UIPresence):
        def heartbeat(self, page):
            super().heartbeat(page)
            with self._lock:
                self._startup_until = 0

    web_app._UIPresence = DocumentReadyPresence
    web_app._UI_CLOSE_GRACE_S = 5
    # Keep the seeded recap; spawned processes do not inherit pytest's worker patches.
    # The real idle monitor still runs independently of background preparation.
    web_app.BACKGROUND_WORK = False
    store = Store(":memory:")
    try:
        store.init_schema()
        app = _recap_app(store, _STORY)
        ready.send('fixture-ready')
        ready.close()
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        ready.close()
        store.conn.close()


@pytest.fixture
def packaged_recap(tmp_path, request):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    context = multiprocessing.get_context("spawn")
    ready, seeded = context.Pipe(duplex=False)
    report = Path(request.config.getoption('output')).resolve() / 'recap-coverage' / request.node.name
    coverage = ChildCoverage.optional(request, tmp_path, report)
    control, child_control = context.Pipe() if coverage else (None, None)
    args = (port, str(tmp_path), seeded)
    process = (context.Process(target=run_spawned, args=(coverage.specification(), child_control, _serve_recap, args))
               if coverage else context.Process(target=_serve_recap, args=args))
    process.start()
    seeded.close()
    if child_control is not None:
        child_control.close()
    phases = SpawnedCoveragePhases(request.node, process, control) if coverage else None
    completed = False
    try:
        # Schema creation and story seeding precede network startup. Synchronize
        # those phases instead of spending the socket deadline on fixture work.
        assert ready.poll(60), 'packaged fixture did not finish seeding'
        assert ready.recv() == 'fixture-ready'
        deadline = time.monotonic() + 15
        while True:
            assert process.is_alive(), "packaged server exited before startup"
            try:
                with urlopen(base + "/bridge/status", timeout=0.2):
                    break
            except URLError:
                assert time.monotonic() < deadline, "packaged server did not start"
                time.sleep(0.02)
        if coverage:
            request.config.pluginmanager.register(phases, 'recap-' + coverage.directory.name)
        yield base, process
        completed = not process.is_alive()
    finally:
        if phases is not None:
            request.config.pluginmanager.unregister(phases)
        try:
            if coverage and process.is_alive():
                switch_spawned_phase(control, 'teardown')
        finally:
            ready.close()
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            assert not process.is_alive(), 'Owned recap server did not stop'
            if control is not None:
                control.close()
            if coverage:
                coverage.finish(process, completed=completed)


def test_recap_keeps_packaged_server_alive_through_download_and_exit(packaged_recap, page, tmp_path):
    base, process = packaged_recap
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    page.goto(base + "/#notices")
    expect(page.locator("#recap-nudge")).to_be_visible()
    # The standalone reel registers its own document lease while Home's lease expires.
    with page.expect_response(lambda r: r.url.startswith(base + "/heartbeat?") and r.status == 204):
        page.locator('#recap-nudge a[href="/trends/story/2026-06"]').click()
    page.wait_for_timeout(6500)  # pass the fixture's close grace while the reel is open
    assert process.is_alive()
    page.get_by_role("button", name=f"Beat {len(_STORY['beats'])}", exact=True).click()
    save = page.get_by_role("button", name="Save image", exact=True)
    expect(save).to_be_visible()
    with page.expect_download() as download_info:
        save.click()
    download = download_info.value
    assert download.suggested_filename == "your-june.png"
    image = tmp_path / download.suggested_filename
    download.save_as(image)
    assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert page.url == base + "/trends/story/2026-06"
    with page.expect_response(lambda r: r.url.startswith(base + "/heartbeat?") and r.status == 204):
        page.get_by_role("link", name="Close", exact=True).click()
    assert page.url == base + "/"
    expect(page.locator("#recap-nudge")).to_have_count(0)
    assert process.is_alive()
    assert errors == []
    # Retain normal packaged behavior: closing the actual UI tab should still stop the server.
    page.close()
    process.join(timeout=8)
    assert not process.is_alive()


def test_closing_one_ui_tab_preserves_the_other(packaged_recap, page):
    base, process = packaged_recap
    page.goto(base + "/")
    other = page.context.new_page()
    other.goto(base + "/")
    page.close()
    try:
        other.wait_for_timeout(6500)
        assert process.is_alive()
        other.reload()
        assert other.locator("main").is_visible()
    finally:
        other.close()
    process.join(timeout=8)
    assert not process.is_alive()


def _settled_slide(page, index):
    slide = page.locator('.slide').nth(index)
    # Observe the real entrance on animation frames instead of waiting for a
    # remote assertion's next retry. Keep the independent assertions below.
    page.wait_for_function('''index => {
        const slides = [...document.querySelectorAll('.slide')];
        const visible = slides.filter(el => {
            const bounds = el.getBoundingClientRect();
            return bounds.width > 0 && bounds.height > 0
                && getComputedStyle(el).visibility === 'visible';
        });
        return visible.length === 1 && visible[0] === slides[index]
            && getComputedStyle(visible[0]).opacity === '1';
    }''', arg=index, polling='raf', timeout=5000).dispose()
    expect(slide).to_be_visible()
    # Alpine can briefly show both slides while applying x-show and the real
    # entrance transition. Wait for uniqueness before checking its content.
    expect(page.locator('.slide:visible')).to_have_count(1)
    expect(slide).to_have_css('opacity', '1')
    return slide


def test_discoveries_have_one_subject_no_links_and_normal_reel_navigation(live_app, store, page):
    story = _detailed_story()
    store.rec_proposals.put_proposals("trend_rollups", {"story": story}, 1)
    page.goto(live_app + "/trends/story/2026-06")
    index = next(i for i, b in enumerate(story["beats"]) if b["kind"] == "new_this_month")
    page.get_by_role("button", name=f"Beat {index + 1}", exact=True).click()
    slide = _settled_slide(page, index)
    expect(slide.get_by_role('heading', level=1)).to_have_text('An album with a long title')
    expect(page.locator('.recap-details a')).to_have_count(0)
    page.keyboard.press("ArrowRight")
    slide = _settled_slide(page, index + 1)
    expect(slide.get_by_role('heading', level=1)).to_have_text('September evenings')
    page.keyboard.press('ArrowRight')
    slide = _settled_slide(page, index + 2)
    expect(slide.get_by_role('heading', level=1)).to_have_text('A newly heard artist')
    page.keyboard.press("ArrowLeft")
    slide = _settled_slide(page, index + 1)
    expect(slide.get_by_role('heading', level=1)).to_have_text('September evenings')
    assert page.url == live_app + '/trends/story/2026-06'


@pytest.mark.parametrize('viewport', [{'width': 1024, 'height': 600}, {'width': 1700, 'height': 1100}])
def test_october_album_title_fits_without_scrolling(live_app, store, page, viewport):
    # The album and coverage shown in the user's screenshot and confirmed by
    # the read-only October snapshot; isolated publication for browser rendering.
    story = {**_STORY, 'beats': [_STORY['beats'][0], {
        'kind': 'new_this_month', 'title': 'New album', 'entry': {
            'title': 'Colorscapes Volume Eight - Mixed by PRAANA & Estiva',
            'subtitle': 'PRAANA, Estiva', 'detail': '24 of 39 tracks played'}}, _STORY['beats'][-1]]}
    store.rec_proposals.put_proposals('trend_rollups', {'story': story}, 1)
    page.set_viewport_size(viewport)
    page.goto(live_app + '/trends/story/2026-06')
    page.keyboard.press('ArrowRight')
    slide = page.locator('.slide-new_this_month')
    expect(slide).to_have_css('opacity', '1')
    expect(slide.get_by_role('heading', level=1)).to_have_text(story['beats'][1]['entry']['title'])
    assert slide.locator('.recap-details').evaluate('''el => el.scrollHeight <= el.clientHeight + 1
        && el.scrollWidth <= el.clientWidth + 1 && [...el.children].every(child => {
            const r = child.getBoundingClientRect();
            return r.top >= 70 && r.bottom <= innerHeight - 40 && r.left >= 0 && r.right <= innerWidth;
        })''')
    page.screenshot(path=f'/tmp/tuneconsole-863-october-album-{viewport["width"]}.png')


@pytest.mark.parametrize("viewport", [{'width': 1280, 'height': 720}, {'width': 1920, 'height': 1080}])
def test_optional_screens_fit_and_navigate(live_app, store, page, viewport, tmp_path, record_property):
    @contextmanager
    def phase(name):
        began = time.perf_counter()
        try:
            yield
        finally:
            record_property('recap_optional_' + name + '_seconds', time.perf_counter() - began)

    with phase('publication'):
        story = _detailed_story()
        store.rec_proposals.put_proposals("trend_rollups", {"story": story}, 1)
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    page.set_viewport_size(viewport)
    with phase('navigation'):
        page.goto(live_app + "/trends/story/2026-06")
    expect(page.get_by_role("button", name="Back", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Continue", exact=True)).to_have_count(0)
    for index, beat in enumerate(story['beats']):
        kind = beat['kind']
        if kind not in ('new_this_month', 'different', 'same'):
            continue
        label = f'{kind}_{index}'
        with phase(label + '_select'):
            page.get_by_role("button", name=f"Beat {index + 1}", exact=True).click()
            slide = _settled_slide(page, index)
        with phase(label + '_layout'):
            expect(slide.get_by_role("heading", level=1)).to_be_in_viewport()
            panel = slide.locator(".recap-details")
            measured = panel.evaluate('''el => {
                const bounds = el.getBoundingClientRect();
                return {
                    bounds: {x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height},
                    contentFits: el.scrollHeight <= el.clientHeight + 1
                        && el.scrollWidth <= el.clientWidth + 1
                        && [...el.children].every(child => {
                            const r = child.getBoundingClientRect();
                            return r.top >= 70 && r.bottom <= innerHeight - 40
                                && r.left >= 0 && r.right <= innerWidth;
                        })
                };
            }''')
            bounds = measured['bounds']
            assert bounds['height'] > 0
            assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= viewport["height"]
            assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= viewport["width"]
            expect(panel.get_by_role('heading', level=1)).to_have_count(1)
            expect(panel.get_by_role('link')).to_have_count(0)
            assert measured['contentFits']
        with phase(label + '_screenshot'):
            page.screenshot(path=f'/tmp/tuneconsole-863-{kind}-{index}-{viewport["width"]}.png')
        # Tapping the panel's padding advances just like tapping the background.
        with phase(label + '_panel_forward'):
            page.mouse.click(bounds['x'] + bounds['width'] * .75, bounds['y'] + 5)
            after = _settled_slide(page, index + 1)
            expect(after).to_have_attribute('aria-label', story['beats'][index + 1]['kind'])
        with phase(label + '_background_back'):
            page.mouse.click(10, viewport["height"] * .6)
            slide = _settled_slide(page, index)
            expect(slide.get_by_role('heading', level=1)).to_have_text(beat['entry']['title'])
        # The left side of a panel also follows the normal back-tap convention.
        with phase(label + '_panel_back'):
            page.mouse.click(bounds['x'] + 20, bounds['y'] + 5)
            before = _settled_slide(page, index - 1)
            expect(before).to_have_attribute('aria-label', story['beats'][index - 1]['kind'])
    assert errors == []
