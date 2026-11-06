"""Exercise inline artist setup against the real routes and an isolated YouTube fixture."""
import hashlib
import inspect
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.browser_fixtures import _free_port
from tests.live_server import LiveServer
from tests.test_artist_start import Catalog, page as artist_page
from yt_playlist.web.app import create_app
from tests.browser.quiet_action_support import (  # noqa: F401 - shared evidence fixture
    action_evidence, assert_quiet_action, fail_action,
)


@pytest.fixture
def artist_flow_evidence(page, tmp_path):
    root = Path(__file__).resolve().parents[2]
    static = root / 'src/yt_playlist/web/static'
    assets = {name: (static / name).read_bytes() for name in (
        'artist-start.js', 'artist-start.css', 'account-growth.js',
        'account-growth.css', 'home.js', 'onboarding.css', 'playlist-generation.js')}
    paths = [root / name for name in (
        'src/yt_playlist/rec/artist_start.py',
        'src/yt_playlist/rec/onboarding.py',
        'src/yt_playlist/rec/account_growth.py',
        'src/yt_playlist/web/app.py',
        'src/yt_playlist/web/routes/home.py',
        'src/yt_playlist/web/routes/home_artist_start.py',
        'src/yt_playlist/web/routes/home_generation.py',
        'src/yt_playlist/web/routes/home_onboarding.py',
        'src/yt_playlist/web/templates/_partials/artist_playback.html',
        'src/yt_playlist/web/templates/artist_start.html',
        'src/yt_playlist/web/templates/home.html',
        'src/yt_playlist/web/templates/_partials/account_growth.html',
        'src/yt_playlist/web/templates/_partials/onboarding_start.html',
    )] + [static / name for name in assets] + [Path(__file__).resolve()]
    hashes = lambda: {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in paths}
    report = {'status': 'incomplete', 'source_before': hashes(), 'served_assets': {},
              'loaded_create_app': str(Path(inspect.getsourcefile(create_app)).resolve())}
    assert Path(report['loaded_create_app']) == root / 'src/yt_playlist/web/app.py'

    def served(response):
        name = urlsplit(response.url).path.removeprefix('/static/')
        if name in assets:
            report['served_assets'].setdefault(name, []).append(
                {'status': response.status, 'sha256': hashlib.sha256(response.body()).hexdigest()})

    page.on('response', served)
    try:
        yield report
    finally:
        page.remove_listener('response', served)
        report['source_after'] = hashes()
        report['source_unchanged'] = report['source_before'] == report['source_after']
        (tmp_path / 'artist-first-listen-evidence.json').write_text(json.dumps(report, indent=2))
        if report.get('report_path'):
            Path(report['report_path']).write_text(json.dumps(report, indent=2))
        assert report['source_unchanged']
        if report['status'] == 'passed':
            assert report['served_assets'].keys() == assets.keys()
            for name, responses in report['served_assets'].items():
                assert all(response == {'status': 200, 'sha256': hashlib.sha256(assets[name]).hexdigest()}
                           for response in responses)


@pytest.fixture
def artist_app(store):
    identity = store.accounts.upsert_account('main', 'cred', None, True)
    store.settings.set_setting('last_sync_at', '999')
    class SearchableCatalog(Catalog):
        def search(self, query, filter='songs', limit=8):
            return [{'artist': row['name'], 'browseId': browse_id}
                    for browse_id, row in self._artists.items()
                    if query.casefold() in row['name'].casefold()][:limit]

    fake = SearchableCatalog()
    for index in range(8):
        fake._artists[f'UCartist{index}'] = artist_page('Artist ' + str(index), index, related=[
            {'title': 'Artist ' + str(other), 'browseId': f'UCartist{other}'}
            for other in range(8) if other != index])
        fake._artists[f'UCartist{index}']['thumbnails'] = [{'url': 'https://example.com/artist.jpg'}]
        # The controlled provider must be able to save and read back the same
        # songs it offered through artist browsing.
        for song in fake._artists[f'UCartist{index}']['songs']['results']:
            fake._catalog[song['videoId']] = song
    app = create_app(store, lambda: {identity: fake}, now_fn=lambda: 1000., background_work=False)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    with LiveServer(server):
        yield f'http://127.0.0.1:{port}'


def _save_artists(page, base, count):
    for index in range(count):
        response = page.request.post(base + '/home/onboard/artists/select', data={
            'browse_id': f'UCartist{index}', 'selected': True})
        assert response.ok










@pytest.mark.parametrize('width', [1440])
@pytest.mark.parametrize('catalog_state', ['empty'])
def test_required_picker_suggestions_spotify_draft_and_first_listen(
        artist_app, store, page, width, catalog_state, tmp_path, artist_flow_evidence):
    if catalog_state in ('partial_history', 'incidental_live', 'unread_albums'):
        from tests.test_artist_start import EmptyLibraryCatalog
        from yt_playlist.library.sync import sync_all

        fake = EmptyLibraryCatalog()
        fake.albums_readable = catalog_state != 'unread_albums'
        with store._lock:
            store.settings.delete_setting('last_sync_at')
            outcome = sync_all(store, {1: fake}, 1000.)
        assert outcome['status'] == 'partial'
        assert store.settings.get_setting('last_sync_at') is None
        assert set(dict(store.history.recent_plays_with_ts())) == {'one past song|past artist'}
        if catalog_state == 'incidental_live':
            for index in range(5):
                store.history.record_play_event(1, f'incidental {index}|past artist', f'incidental-{index}', 900. + index)
    artist_flow_evidence['catalog_state'] = catalog_state
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('https://example.com/**', lambda route: route.fulfill(
        path='src/yt_playlist/web/static/onboarding-albums.png', content_type='image/png'))
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True}))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(artist_app)
    expect(page.get_by_role('heading', name='Pick 3 artists you love.')).to_be_visible()
    expect(page.get_by_role('tab', name='On air', exact=True)).to_have_count(0)
    expect(page.get_by_role('region', name='Festival roadmap', exact=True)).to_be_visible()
    expect(page.locator('.festival-intro-sub')).to_have_text('Choose your artists')
    expect(page.locator('.arrival-progress')).to_be_hidden()
    expect(page.locator('.artist-start .artist-start-primary:visible')).to_have_count(0)
    assert page.locator('.setup-listening > [data-onboarding-artists]').count() == 1
    assert page.locator('.artist-start').evaluate('el => el.getBoundingClientRect().top >= document.querySelector(".festival-intro").getBoundingClientRect().bottom')
    page.screenshot(path=tmp_path / f'artist-in-festival-{catalog_state}-{width}.png', full_page=True)
    page.locator('button.festival-first-set').click()
    search = page.get_by_role('searchbox', name='Search YouTube Music artists')
    expect(search).to_be_focused()
    search.fill('artist')
    page.get_by_role('button', name='Choose Artist 0', exact=True).click()
    page.get_by_role('button', name='Choose Artist 1', exact=True).click()
    page.get_by_role('button', name='Coming from Spotify?').click()
    expect(page.get_by_role('heading', name='Your old favorites can come too.')).to_be_visible()
    page.screenshot(path=f'/tmp/tuneconsole-791-spotify-{catalog_state}-{width}.png', full_page=True)
    page.get_by_role('button', name='How to request my files').click()
    expect(page.get_by_text('Extended streaming history:', exact=False)).to_be_visible()
    page.get_by_role('button', name='Back to my artists').click()
    page.reload()
    expect(page.get_by_role('button', name='Remove Artist 0', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Remove Artist 1', exact=True)).to_be_visible()
    page.get_by_role('button', name='Choose Artist 2', exact=True).click()
    page.get_by_role('button', name='Find my first set', exact=True).click()
    expect(page.get_by_role('button', name='Play my first set', exact=True)).to_be_visible()
    assert len(store.discovery.starter_artists()) == 3
    assert store.conn.execute('SELECT COUNT(*) FROM play_events').fetchone()[0] == (5 if catalog_state == 'incidental_live' else 0)
    assert page.locator('body').evaluate('(body) => body.scrollWidth <= innerWidth')
    assert page.locator('.artist-start h2:visible').evaluate('(el) => getComputedStyle(el).textAlign') == 'left'
    expect(page.locator('.festival-intro-sub')).to_have_text('Your first set is ready')
    page.screenshot(path=tmp_path / f'artist-picks-{catalog_state}-{width}.png', full_page=True)
    page.get_by_role('button', name='Play my first set', exact=True).click()
    expect(page.get_by_role('heading', name='Your music is on its way.')).to_be_visible()
    assert page.url.rstrip('/') == artist_app
    page.reload()
    expect(page.get_by_role('heading', name='Your first set is waiting.')).to_be_visible()
    page.evaluate('window.artistDocument = document; window.artistMap = document.querySelector(".festival-map"); window.artistPlayer = document.querySelector(".global-now-playing")')
    playing = {'connected': True, 'now_playing': {'video_id': 'song00', 'title': 'Song 0-0',
               'artist': 'Artist 0', 'paused': False, 'playlist_ytm_id': 'PL_NEW_0'}}
    page.evaluate('(data) => window.dispatchEvent(new CustomEvent("bridge-status", {detail:data}))', playing)
    expect(page.get_by_role('heading', name='Your first set is waiting.')).to_be_visible()
    # A playback snapshot alone cannot graduate setup before durable live evidence.
    store.history.record_play_event(1, 'song 0-0|artist 0', 'song00', 1001, playlist_ytm_id='PL_NEW_0')
    page.evaluate('(data) => window.dispatchEvent(new CustomEvent("bridge-status", {detail:data}))', playing)
    expect(page.get_by_role('region', name='Festival roadmap', exact=True)).to_be_visible()
    first_set = page.locator('button.festival-first-set')
    expect(first_set).to_have_count(1)
    expect(first_set).to_have_accessible_name('Go to my first set')
    expect(first_set).to_be_visible()
    map_target = page.locator('.festival-destination[aria-label="Go to my first set"]')
    expect(map_target).to_have_count(1)
    expect(map_target).to_be_visible()
    expect(page.get_by_role('heading', name='Pick 3 artists you love.', exact=True)).to_have_count(0)
    expect(page.locator('#gen-onboard_library')).to_be_visible()
    expect(page.locator('#gen-onboard_library .starter-track')).to_have_count(12)
    assert page.evaluate('artistDocument === document && artistMap === document.querySelector(".festival-map") && artistPlayer === document.querySelector(".global-now-playing")')
    # The first artist set is useful output, not the four eligible prepared mixes
    # required to unlock the Mix tapes destination (#564).
    expect(page.get_by_role('tab', name='Mix tapes', exact=True)).to_have_count(0)
    growth = page.request.get(artist_app + '/home/growth')
    assert growth.status == 200
    artist_flow_evidence['growth_after_first_play'] = growth.json()
    assert not artist_flow_evidence['growth_after_first_play']['ready_formats']['mixes']
    # Saving the first set starts an optional achievement, but its distant
    # route must stay dark until it is explored. Only the connected core
    # preparation frontier receives the blue path light.
    frontier = page.locator('.festival-routes .growth-route.is-preparing:visible')
    expect(frontier).to_have_count(1)
    expect(frontier).to_have_attribute('data-destination', 'foundation')
    expect(page.locator('.festival-routes [data-destination="milestones"]')).to_be_hidden()
    assert page.url.rstrip('/') == artist_app
    assert len(store.discovery.starter_artists()) == 3
    assert page.locator('body').evaluate('body => body.scrollWidth <= innerWidth')
    artist_flow_evidence['geometry_after_first_play'] = page.evaluate('''() => ({
        viewport: {width: innerWidth, height: innerHeight},
        bodyWidth: document.body.scrollWidth,
        map: document.querySelector('.festival-map-scroll').getBoundingClientRect().toJSON(),
        starter: document.querySelector('#gen-onboard_library').getBoundingClientRect().toJSON()
    })''')
    page.screenshot(path=tmp_path / f'artist-first-listen-durable-{width}.png', full_page=True)
    assert not errors
    artist_flow_evidence['status'] = 'passed'
