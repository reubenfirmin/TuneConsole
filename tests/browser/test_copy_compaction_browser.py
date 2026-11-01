"""Rendered maintenance, confirmed feedback and resumed service after replacement."""
import threading
import time

from playwright.sync_api import expect
import pytest
import uvicorn

from tests.browser_fixtures import _free_port
from tests.conftest import FakeClient
from tests.test_deferred_playback_feedback import library
from yt_playlist.core import playback_feedback
from yt_playlist.core.db_rebuild import build as retained_copy
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('width,height,font', [(1280, 720, 16), (360, 720, 24)])
def test_banner_and_like_across_real_copy(library, page, tmp_path, monkeypatch, width, height, font):
    store, main, other, _, _ = library
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
        page.goto(url + '/track/known')
        page.add_style_tag(content=f'html {{ font-size: {font}px; }}')
        banner = page.locator('#storage-maintenance')
        expect(banner).to_have_text('Library updates are paused.')
        expect(banner).to_be_visible()
        expect(page.locator('html')).to_have_attribute('data-storage-compacting', 'true')
        like = page.locator('.track-detail').get_by_role('button', name='Like', exact=True)
        like.click()
        expect(like).to_have_attribute('aria-pressed', 'true')
        assert remote.rated == [('known', 'LIKE')]
        assert playback_feedback.inbox(store).pending() == 1
        page.screenshot(path=str(tmp_path / 'active-copy.png'), full_page=True)
        release.set()
        assert maintenance.ready.wait(45)
        assert maintenance.phase == 'done'
        expect(banner).to_be_hidden(timeout=10000)
        assert not playback_feedback.inbox(store).pending()
        assert store.tracks.item_rating(main, 'known')['status'] == 'LIKE'
        page.reload()
        expect(like).to_have_attribute('aria-pressed', 'true')
        page.screenshot(path=str(tmp_path / 'resumed-copy.png'), full_page=True)
    finally:
        release.set()
        server.should_exit = True
        thread.join(15)
        assert not thread.is_alive()
