"""Earned destinations stay navigable while their outputs are being replaced."""
import threading

from playwright.sync_api import expect
import uvicorn

from tests.browser_fixtures import _free_port
from tests.live_server import LiveServer
from tests.conftest import FakeClient
from tests.test_deferred_playback_feedback import library
from yt_playlist.core.db_rebuild import build
from yt_playlist.web.app import create_app


def test_missing_outputs_do_not_remove_earned_home_during_maintenance(library, page, monkeypatch):
    # Confirmed Vibes use restores the preceding main path. No controlled ready
    # response or prepared menu hides the missing-output case in this fixture.
    store, main, other, _, _ = library
    store.rec_proposals.put_proposals('listening_choices', {'home_format': {
        'vibe_episodes': 1}}, 1)
    store.settings.set_setting('home_format', 'mixes')
    app = create_app(store, lambda: {main: FakeClient(), other: FakeClient()},
                     now_fn=lambda: 1., background_work=False)
    maintenance = app.state.ctx.storage_maintenance
    entered, release = threading.Event(), threading.Event()
    def interrupted(*args, **kwargs):
        entered.set()
        assert release.wait(60)
        raise InterruptedError('Controlled candidate build failure')
    monkeypatch.setattr(build, 'build', interrupted)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': 1440, 'height': 1100})
    owner = LiveServer(server)
    try:
        url = f'http://127.0.0.1:{port}'
        page.goto(url)
        mixes = page.get_by_role('tab', name='Mix tapes', exact=True)
        expect(mixes).to_have_attribute('aria-selected', 'true')
        expect(page.locator('[data-dash="directions"] .mix-preparation-note')).to_be_visible()
        expect(page.get_by_role('tab', name='On air', exact=True)).to_be_visible()
        expect(page.get_by_role('tab', name='TV', exact=True)).to_be_visible()
        expect(page.get_by_role('tab', name='Revisit', exact=True)).to_be_hidden()
        expect(page.get_by_role('tab', name='Into recently', exact=True)).to_be_hidden()
        assert page.request.post(url+'/storage-maintenance/compact').status == 202
        assert entered.wait(15)
        page.reload()
        expect(mixes).to_have_attribute('aria-selected', 'true')
        page.get_by_role('tab', name='TV', exact=True).click()
        expect(page.locator('[data-dash="tv"]')).to_be_visible()
        page.reload()
        expect(page.get_by_role('tab', name='TV', exact=True)).to_have_attribute('aria-selected', 'true')
        mixes.click()
        expect(mixes).to_have_attribute('aria-selected', 'true')
        expect(page.locator('[data-dash="tv"]')).to_be_hidden()
        expect(page.locator('[data-dash="directions"] .mix-preparation-note')).to_be_visible()
        page.screenshot(path='/tmp/tuneconsole-649-earned-maintenance.png', full_page=True, animations='disabled')
        release.set()
        assert maintenance.ready.wait(30)
        assert maintenance.phase == 'deferred'
        assert not store.activity_paused
        # A failed build resumes recording against the same source. Its missing
        # output must not reset Home or manufacture an unlock notice.
        page.reload()
        expect(mixes).to_have_attribute('aria-selected', 'true')
        expect(page.locator('.growth-unlock-notice')).to_be_hidden()
        assert not errors
    finally:
        release.set()
        owner.close()
