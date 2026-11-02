"""Rendered maintenance, confirmed feedback and resumed service after replacement."""
import threading
import time

from playwright.sync_api import expect
import pytest
import uvicorn

from tests.browser_fixtures import _free_port
from tests.conftest import FakeClient
from tests.test_deferred_playback_feedback import library
from tests.home_fixture_support import returning_listener
from yt_playlist.core import playback_feedback
from yt_playlist.core.db_rebuild import build as retained_copy
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('width,height,font', [(1280, 720, 16)])
def test_banner_and_like_across_real_copy(library, page, monkeypatch, width, height, font):
    store, main, other, _, _ = library
    returning_listener(store, 'mixes')
    store.tracks.upsert_track('known', 'Song', 'Artist', None, 180,
                              thumbnail='/static/onboarding-mixes.png')
    remote = FakeClient()
    app = create_app(store, lambda: {main: remote, other: FakeClient()}, background_work=False)
    maintenance = app.state.ctx.storage_maintenance
    entered, release = threading.Event(), threading.Event()
    original = retained_copy.build
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(60)
        return original(*args, **kwargs)
    monkeypatch.setattr(retained_copy, 'build', held)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(.02)
        url = f'http://127.0.0.1:{port}'
        assert page.request.post(url + '/storage-maintenance/compact').status == 202
        assert entered.wait(15)
        page.set_viewport_size(dict(width=width, height=height))
        page.add_init_script('document.addEventListener("DOMContentLoaded", () => '
                             f'document.documentElement.style.fontSize = "{font}px");')
        page.goto(url + '/track/known')
        assert page.evaluate('parseFloat(getComputedStyle(document.documentElement).fontSize)') == font
        banner = page.locator('#storage-maintenance')
        expect(banner).to_have_text('Updating library storage; browsing and playback remain available.')
        expect(banner).to_be_visible()
        expect(page.locator('html')).to_have_attribute('data-storage-compacting', 'true')
        like = page.locator('.track-detail').get_by_role('button', name='Like', exact=True)
        like.click()
        expect(like).to_have_attribute('aria-pressed', 'true')
        assert page.locator('.track-detail-art').evaluate('img => img.complete && img.naturalWidth > 0')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert remote.rated == [('known', 'LIKE')]
        assert playback_feedback.inbox(store).pending() == 1
        page.evaluate('scrollTo(0, 0)')
        page.screenshot(path=f'/tmp/tuneconsole-740-active-{width}-{font}.png', full_page=True, animations='disabled')
        release.set()
        assert maintenance.ready.wait(45)
        assert maintenance.phase == 'done'
        expect(banner).to_be_hidden(timeout=10000)
        assert not playback_feedback.inbox(store).pending()
        assert store.tracks.item_rating(main, 'known')['status'] == 'LIKE'
        page.reload()
        expect(like).to_have_attribute('aria-pressed', 'true')
        assert page.evaluate('parseFloat(getComputedStyle(document.documentElement).fontSize)') == font
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=f'/tmp/tuneconsole-740-resumed-{width}-{font}.png', full_page=True, animations='disabled')
    finally:
        release.set()
        server.should_exit = True
        thread.join(15)
        assert not thread.is_alive()
