"""Build-time Vibes survive real mix creation and later Home changes."""
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.conftest import FakeClient
from tests.home_fixture_support import browser_store as store
from tests.playlist_fixture_support import remote_catalog
from yt_playlist.rec import home_vibe


@pytest.fixture
def vibe_creation_app(request, monkeypatch, store, measured_browser_home_store, unlocked_home_controls):
    from yt_playlist.web import app as app_module
    original, apps = app_module.create_app, []
    def capture(*args, **kwargs):
        app = original(*args, **kwargs)
        apps.append(app)
        return app
    monkeypatch.setattr(app_module, 'create_app', capture)
    base = request.getfixturevalue('live_app')
    account = store.accounts.get_master_account().id
    provider = FakeClient()
    provider._catalog.update(remote_catalog([dict(row) for row in store.conn.execute(
        'SELECT video_id,title,artist,album,thumbnail FROM catalog_tracks WHERE video_id IS NOT NULL')]))
    apps[0].state.ctx.client_provider = lambda: {account: provider}
    return base, provider


def test_every_vibe_title_and_badge_survive_switching_and_clearing(
        vibe_creation_app, store, page):
    base, provider = vibe_creation_app
    page.set_viewport_size({'width': 1600, 'height': 1100})
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    for preset, config in home_vibe.PRESETS.items():
        label = config['label']
        page.goto(base)
        page.get_by_role('button', name='Tune these mixes', exact=True).click()
        with page.expect_response('**/home/vibe') as selected:
            page.get_by_role('dialog', name='Tune these mixes', exact=True).get_by_role(
                'radio', name=label, exact=True).check()
        assert selected.value.status == 200
        board = page.locator('#home-mode-cards')
        expect(board).to_have_attribute('data-vibe', preset)
        bodies = board.locator('.gen-card-body')
        expect(bodies).to_have_count(4)
        assert all(' - ' + label + ' - ' in body.get_attribute('data-name') for body in bodies.all())
        card = page.locator('#gen-wheelhouse')
        title = card.locator('.gen-card-body').get_attribute('data-name')
        card.locator('.gen-poster').click()
        with page.expect_popup() as opened:
            card.get_by_role('button', name='Save and play this mix', exact=True).click()
        page.wait_for_url(re.compile(r'/playlist/\d+$'))
        url = page.url
        expect(page.locator('.pl-vibe-badge')).to_have_text(label)
        expect(page.locator('.pl-vibe-badge')).to_be_visible()
        page.wait_for_function('!window.__ytTab || window.__ytTab.closed')
        assert opened.value.is_closed()
        assert provider.created[-1][1] == title + ' #1'
        playlist = store.playlists.get_playlist(int(url.rsplit('/', 1)[1]))
        assert playlist.title == provider.created[-1][1]
        assert store.playlist_recipes.get_recipe(playlist.playlist_scope_id)['vibe'] == preset
        if preset in ('concentrate', 'release'):
            page.screenshot(path=f'/tmp/tuneconsole-869-{preset}.png', full_page=True)
        other = 'energize' if preset != 'energize' else 'relax'
        assert page.request.post(base + '/home/vibe', form={'vibe': other}).status == 200
        page.reload()
        expect(page.locator('.pl-vibe-badge')).to_have_text(label)
        assert page.request.post(base + '/home/vibe', form={'vibe': ''}).status == 200
        page.reload()
        expect(page.locator('.pl-vibe-badge')).to_have_text(label)
        assert page.locator('#pl-title').get_attribute('data-title') == playlist.title
    assert not errors
    # Verify that the rendered badge uses the current checked-in stylesheet.
    sheet = page.request.get(base + '/static/library-entities.css')
    assert sheet.body() == Path('src/yt_playlist/web/static/library-entities.css').read_bytes()
