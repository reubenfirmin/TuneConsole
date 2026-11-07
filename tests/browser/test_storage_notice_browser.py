"""The one-line storage strip stays readable and clear of navigation during repair."""
import json
from pathlib import Path
import re
import time

from playwright.sync_api import expect
import pytest
import uvicorn

from tests.browser_fixtures import _free_port
from tests.live_server import LiveServer
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
    with LiveServer(server):
        yield f'http://127.0.0.1:{port}', app.state.ctx.storage_maintenance


@pytest.mark.parametrize('width',[1536])
def test_history_notice_with_available_cards_and_writable_library(notice_app,store,page,width):
    url,maintenance = notice_app
    maintenance.ready.clear()
    maintenance.report = {'analytics_pending':True}
    store.rec_proposals.put_proposals('home_prepared',{'schema':1,'protos':[
        {'label':'Available mix','tracks':[{'videoId':'prepared-track'}]}]},1)
    page.set_viewport_size({'width':width,'height':900})
    page.goto(url+'/playlists')
    banner = page.locator('#storage-maintenance')
    expect(banner).to_be_visible()
    expect(banner).to_have_text('Optimizing app storage; some recommendations are temporarily unavailable.')
    expect(banner).to_have_attribute('role','status')
    assert page.request.post(url+'/settings/lastfm-key',form={'key':'accepted-during-history'}).status==204
    assert store.settings.get_setting('lastfm_api_key')=='accepted-during-history'
    assert banner.evaluate('el => el.scrollWidth <= el.clientWidth')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=f'/tmp/tuneconsole-686-history-{width}.png',full_page=True)
    maintenance.report['analytics_pending'] = False
    maintenance.ready.set()
    expect(banner).to_be_hidden()


def test_cleanup_pending_after_recovery_does_not_keep_the_frontend_paused(notice_app, store, page):
    url, maintenance = notice_app
    maintenance.active = True
    store._evidence_recovery_pending = True
    page.goto(url + '/playlists')
    banner = page.locator('#storage-maintenance')
    expect(banner).to_have_text('Optimizing app storage; some recommendations are temporarily unavailable.')
    page.wait_for_function('document.documentElement.dataset.storageCompacting === "true"')
    maintenance.active = False
    # The existing page must recover while cleanup still awaits another worker
    # turn. Clearing pending intent or restarting the frontend is unnecessary.
    expect(banner).to_be_hidden()
    page.wait_for_function('document.documentElement.dataset.storageCompacting === "false"')
    status = page.request.get(url + '/storage-maintenance/status').json()
    assert status['evidence_maintenance']['pending'] and status['recording'] and not status['active']
    assert page.request.post(url + '/settings/lastfm-key', form={'key': 'cleanup-pending'}).status == 204
    identity = store.accounts.get_master_account().id
    playback_feedback.capture(store, 'player', identity, {'kind': 'state', 'state': 'paused'}, 10)
    assert playback_feedback.drain(store) == 1
    page.screenshot(path='/tmp/tuneconsole-866-retention-resumed.png', full_page=True)


@pytest.mark.parametrize('width,text_size', [(1536, 16)])
def test_single_line_notice_with_sidebar_and_without_polling(notice_app, store, page, tmp_path, width, text_size, record_property):
    url, maintenance = notice_app
    timings, polls = {}, 0

    def measured(name, operation, *args, **kwargs):
        started = time.perf_counter()
        try:
            return operation(*args, **kwargs)
        finally:
            values = timings.setdefault(name, [0, 0.])
            values[0] += 1
            values[1] += time.perf_counter() - started
            record_property('notice_phase_costs', json.dumps(timings, sort_keys=True))

    def poll():
        nonlocal polls
        def deliver():
            with page.expect_response(lambda response: response.url.endswith('/storage-maintenance/status')) as response:
                measured('poll_clock_delivery', page.clock.run_for, 2000)
            return response.value.json()
        state = measured('status_poll', deliver)
        polls += 1
        record_property('notice_delivered_polls', polls)
        return state

    maintenance.active = True
    page.set_viewport_size({'width': width, 'height': 900})
    # Advance only the polling schedule. Every poll still reaches the real
    # server; production animation and status rendering remain enabled.
    page.clock.install()
    page.add_init_script('document.addEventListener("DOMContentLoaded", () => '
                         f'document.documentElement.style.fontSize = "{text_size}px");')
    with page.expect_response(lambda response: response.url.endswith('/storage-maintenance/status')) as initial:
        measured('navigation', page.goto, url + '/playlists')
    assert initial.value.json()['active']
    served_css = page.request.get(url + '/static/storage-maintenance.css')
    assert served_css.body() == (Path(app_module.__file__).parent / 'static/storage-maintenance.css').read_bytes()
    banner = page.locator('#storage-maintenance')

    def check_notice():
        expect(banner).to_be_visible()
        layout = measured('layout', banner.evaluate, '''el => {
            const range = document.createRange(); range.selectNodeContents(el);
            const box = el.getBoundingClientRect(), rail = document.querySelector('.topbar').getBoundingClientRect();
            return {rootFont:parseFloat(getComputedStyle(document.documentElement).fontSize),
              font:parseFloat(getComputedStyle(el).fontSize),
              box:{x:box.x,width:box.width,height:box.height},rail:{x:rail.x,width:rail.width},
              fits:el.scrollWidth<=el.clientWidth,pageFits:document.documentElement.scrollWidth<=innerWidth,
              lines:range.getClientRects().length};
        }''')
        assert layout['rootFont'] == text_size
        assert layout['font'] >= .875 * text_size
        box = layout['box']
        assert box['x'] >= 0 and box['x'] + box['width'] <= width
        assert box['height'] <= 2.5 * text_size
        assert layout['fits'] and layout['pageFits']
        assert layout['lines'] == 1
        rail = layout['rail']
        assert box['x'] >= rail['x'] + rail['width']

    expect(banner).to_have_text('Optimizing app storage; some recommendations are temporarily unavailable.')
    expect(banner).not_to_have_attribute('title', re.compile('.+'))
    expect(banner).to_have_attribute('role', 'status')
    check_notice()
    assert page.request.post(url + '/sync').status == 503
    measured('screenshot', page.screenshot, path=f'/tmp/tuneconsole-650-active-{width}-{text_size}.png', full_page=True)

    if width == 1536:
        maintenance.active = False
        store._evidence_recovery_pending = True
        store._evidence_recovery_failed = True
        assert poll()['warning']
        expect(banner).to_be_hidden()
        expect(page.locator('#toasts [role="alert"]')).to_have_count(0)
        measured('screenshot', page.screenshot, path=f'/tmp/tuneconsole-879-stalled-{width}.png', full_page=True)
        store._evidence_recovery_failed = False

    store._evidence_recovery_pending = False
    maintenance.active = False
    maintenance.ready.clear()
    assert not poll()['active']
    preparation = 'Preparing library data. Features will appear as they are ready.'
    expect(banner).to_have_text(preparation)
    expect(banner).to_have_attribute('role', 'status')
    check_notice()
    status = page.request.get(url + '/storage-maintenance/status').json()
    assert not status['active'] and status['recording']
    assert page.request.post(url + '/settings/lastfm-key', form={'key': 'notice-test-key'}).status == 204
    assert store.settings.get_setting('lastfm_api_key') == 'notice-test-key'
    account = store.accounts.get_master_account().id
    playback_feedback.capture(store, 'player', account, {'kind': 'state', 'state': 'paused'}, 1)
    assert playback_feedback.drain(store) == 1
    # Preparation has the same useful server-rendered notice without polling.
    page.route('**/static/storage-maintenance.js*', lambda route: route.abort())
    measured('reload', page.reload)
    expect(banner).to_have_text(preparation)
    check_notice()
    measured('screenshot', page.screenshot, path=f'/tmp/tuneconsole-824-preparation-{width}-{text_size}.png', full_page=True)
    page.unroute('**/static/storage-maintenance.js*')
    measured('reload', page.reload)
    maintenance.ready.set()
    poll()
    expect(banner).to_be_hidden()
    if width == 1536:
        account = store.accounts.get_master_account().id
        playback_feedback.capture(store, 'player', account, {'kind': 'state', 'state': 'paused'}, 2)
        assert poll()['pending_feedback'] == 1
        expect(banner).to_be_hidden()
        assert page.request.post(url + '/settings/lastfm-key', form={'key': 'feedback-pending'}).status == 204
        assert playback_feedback.drain(store) == 1
        assert poll()['pending_feedback'] == 0
        expect(banner).to_be_hidden()

    maintenance.phase = 'deferred'
    maintenance.ready.clear()
    maintenance._deferred(db_space.LowDiskSpace('insufficient scratch space'))
    assert poll()['warning']
    expect(banner).to_be_hidden()
    expect(banner).to_have_attribute('role', 'status')
    page.wait_for_function('document.documentElement.dataset.storageCompacting === "false"')
    measured('screenshot', page.screenshot, path=f'/tmp/tuneconsole-879-low-space-{width}.png', full_page=True)

    if width == 1536:
        maintenance._deferred(RuntimeError('interrupted'))
        assert poll()['warning']
        expect(banner).to_be_hidden()
        maintenance.phase = 'schema_retry'
        assert poll()['warning']
        expect(banner).to_be_hidden()
        maintenance.phase = 'deferred'
        maintenance._deferred(db_space.LowDiskSpace('insufficient scratch space'))
        assert poll()['warning']
        expect(banner).to_be_hidden()

    # Diagnostics remain quiet when polling is unavailable as well.
    page.route('**/static/storage-maintenance.js*', lambda route: route.abort())
    measured('reload', page.reload)
    expect(banner).to_be_hidden()
    maintenance.phase, maintenance.message = 'done', ''
    maintenance.ready.set()
    page.unroute('**/static/storage-maintenance.js*')
    measured('reload', page.reload)
    expect(banner).to_be_hidden()
