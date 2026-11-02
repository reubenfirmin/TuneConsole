"""The one-line storage strip stays readable and clear of navigation during repair."""
import threading
import time
import re
from pathlib import Path

from playwright.sync_api import expect
import pytest
import uvicorn

from tests.browser_fixtures import _free_port
from tests.conftest import FakeClient
from yt_playlist.core import db_space, playback_feedback
from yt_playlist.web.app import create_app
from yt_playlist.web import app as app_module


@pytest.fixture
def notice_app(store):
    identity = store.accounts.upsert_account('Main', 'credential', None, True)
    store.playlists.upsert_playlist(identity, 'library', 'My music', 0, '', 1)
    app = create_app(store, lambda: {identity: FakeClient()}, now_fn=lambda: 1, background_work=False)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(.02)
        yield f'http://127.0.0.1:{port}', app.state.ctx.storage_maintenance
    finally:
        server.should_exit = True
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize('width,text_size', [(1536, 16), (1040, 16)])
def test_single_line_notice_with_sidebar_and_without_polling(notice_app, store, page, tmp_path, width, text_size):
    url, maintenance = notice_app
    store._evidence_recovery_pending = True
    page.set_viewport_size({'width': width, 'height': 900})
    page.add_init_script('document.addEventListener("DOMContentLoaded", () => '
                         f'document.documentElement.style.fontSize = "{text_size}px");')
    page.goto(url + '/playlists')
    served_css = page.request.get(url + '/static/storage-maintenance.css')
    assert served_css.body() == (Path(app_module.__file__).parent / 'static/storage-maintenance.css').read_bytes()
    banner = page.locator('#storage-maintenance')

    def check_notice():
        expect(banner).to_be_visible()
        assert page.evaluate('parseFloat(getComputedStyle(document.documentElement).fontSize)') == text_size
        assert banner.evaluate('el => parseFloat(getComputedStyle(el).fontSize)') >= .875 * text_size
        box = banner.bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= width
        assert box['height'] <= 2.5 * text_size
        assert banner.evaluate('el => el.scrollWidth <= el.clientWidth')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert banner.evaluate('''el => {
            const range = document.createRange(); range.selectNodeContents(el);
            return range.getClientRects().length;
        }''') == 1
        rail = page.locator('.topbar').bounding_box()
        assert box['x'] >= rail['x'] + rail['width']

    expect(banner).to_have_text('Updating library storage; browsing and playback remain available.')
    expect(banner).not_to_have_attribute('title', re.compile('.+'))
    expect(banner).to_have_attribute('role', 'status')
    check_notice()
    assert page.request.post(url + '/sync').status == 503
    page.screenshot(path=f'/tmp/tuneconsole-650-active-{width}-{text_size}.png', full_page=True)

    if width == 1536:
        store._evidence_recovery_failed = True
        expect(banner).to_have_text('Library updates need attention.')
        expect(banner).to_have_attribute('role', 'alert')
        check_notice()
        page.screenshot(path=f'/tmp/tuneconsole-650-stalled-{width}-{text_size}.png', full_page=True)
        store._evidence_recovery_failed = False

    store._evidence_recovery_pending = False
    if width == 1536:
        account = store.accounts.get_master_account().id
        playback_feedback.capture(store, 'player', account, {'kind': 'state', 'state': 'paused'}, 2)
        expect(banner).to_have_text('Catching up on your listening.')
        check_notice()
        page.screenshot(path=f'/tmp/tuneconsole-650-feedback-{width}-{text_size}.png', full_page=True)
        assert playback_feedback.drain(store) == 1
        expect(banner).to_be_hidden()

    maintenance.phase = 'deferred'
    maintenance._deferred(db_space.LowDiskSpace('insufficient scratch space'))
    expect(banner).to_have_text('Free disk space to finish.')
    expect(banner).to_have_attribute('role', 'alert')
    page.wait_for_function('document.documentElement.dataset.storageCompacting === "false"')
    check_notice()
    page.screenshot(path=f'/tmp/tuneconsole-650-low-space-{width}-{text_size}.png', full_page=True)

    if width == 1536:
        maintenance._deferred(RuntimeError('interrupted'))
        expect(banner).to_have_text('Library update will retry.')
        check_notice()
        maintenance.phase = 'schema_retry'
        expect(banner).to_have_text('Library update needs a retry.')
        check_notice()
        maintenance.phase = 'deferred'
        maintenance._deferred(db_space.LowDiskSpace('insufficient scratch space'))
        expect(banner).to_have_text('Free disk space to finish.')

    # The deferred warning must remain useful with JavaScript unavailable.
    page.route('**/static/storage-maintenance.js*', lambda route: route.abort())
    page.reload()
    expect(banner).to_have_text('Free disk space to finish.')
    check_notice()
    maintenance.phase, maintenance.message = 'done', ''
    page.unroute('**/static/storage-maintenance.js*')
    page.reload()
    expect(banner).to_be_hidden()
