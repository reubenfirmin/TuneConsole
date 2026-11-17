"""Reset notices use the shared dialog and remain optional across reloads."""
import pytest
import uvicorn
from playwright.sync_api import expect

from tests.browser_fixtures import _free_port
from tests.conftest import FakeClient
from tests.live_server import LiveServer
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


@pytest.fixture
def adopter_app(store):
    identity = store.accounts.upsert_account('Main', 'credential', None, True)
    store.settings.set_setting('early_adopter_notice', 'reset-fixture')
    app = create_app(store, lambda: {identity: FakeClient()}, background_work=False)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    with LiveServer(server):
        yield f'http://127.0.0.1:{port}'


@pytest.mark.parametrize('dismiss', ['button', 'escape', 'offline'])
def test_reset_notice_dismisses_and_stays_dismissed(adopter_app, store, page, dismiss):
    page.set_viewport_size({'width': 1440, 'height': 900})
    if dismiss == 'offline':
        page.route('**/early-adopter-notice/dismiss', lambda route: route.abort())
    page.goto(adopter_app + '/playlists')
    dialog = page.get_by_role('dialog', name='Early adopter notice')
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("We had to reset some of your data to perform a feature upgrade; don't worry, no preferences were lost, as they live in Youtube.")
    expect(dialog.get_by_role('link', name='rf@4rc.io')).to_have_attribute('href', 'mailto:rf@4rc.io')
    if dismiss == 'button':
        page.screenshot(path='/tmp/tuneconsole-1237-early-adopter-notice.png')
    if dismiss == 'escape':
        page.keyboard.press('Escape')
    else:
        dialog.get_by_role('button', name='Got it').click()
    expect(dialog).to_have_count(0)
    page.reload()
    expect(page.get_by_role('dialog', name='Early adopter notice')).to_have_count(0)
    if dismiss != 'offline':
        assert store.settings.get_setting('early_adopter_notice') == ''
