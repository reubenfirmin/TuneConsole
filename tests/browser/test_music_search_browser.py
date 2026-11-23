"""Exercise the shared overlay through real HTTP routes and an isolated catalog."""
from urllib.parse import parse_qs

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.browser_fixtures import _free_port
from tests.live_server import LiveServer
from tests.test_music_search import EntityCatalog, SongCatalog
from yt_playlist.rec import artist_start
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


@pytest.fixture
def music_app(store, without_metadata_providers, request):
    account = store.accounts.upsert_account('main', None, True)
    store.settings.set_setting('last_sync_at', '999')
    catalog = getattr(request, 'param', SongCatalog)()
    catalog._catalog['exact-song'] = catalog.search('favorite')[0]
    app = create_app(store, lambda: {account: catalog}, now_fn=lambda: 1000., background_work=False)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    with LiveServer(server):
        yield f'http://127.0.0.1:{port}', catalog


def prepare_page(page, width):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1080 if width == 1920 else 1000})
    page.route('https://example.com/**', lambda route: route.fulfill(
        path='src/yt_playlist/web/static/onboarding-albums.png', content_type='image/png'))
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True,
        'now_playing': {'video_id': 'still-playing', 'title': 'Still playing', 'artist': 'Artist', 'paused': False}}))
    return errors


def test_sources_and_catalog_sections_finish_independently(music_app, store, page):
    base, _ = music_app
    store.tracks.upsert_track('local-song', 'A local favorite', 'Favorite artist')
    errors = prepare_page(page, 1440)
    held = []
    page.route('**/search/catalog/sections?*', lambda route: held.append(route))
    page.goto(base + '/playlists')
    page.keyboard.press('Control+k')
    dialog = page.locator('#music-search')
    page.get_by_role('searchbox', name='Search music').fill('favorite')
    expect(dialog.locator('#music-library-count')).to_have_text('2')
    expect(dialog.locator('#music-catalog-count')).to_have_text('1')
    assert held
    dialog.get_by_role('tab', name='YouTube Music', exact=True).click()
    expect(dialog.locator('[data-video-id="exact-song"]')).to_be_visible()
    expect(dialog.get_by_text('Searching YouTube Music…', exact=True)).to_be_visible()
    held.pop().fulfill(json={'artists': [], 'playlists': [], 'albums': [{
        'browse_id': 'MPREfavorite', 'title': 'Favorite album', 'sub': 'Favorite artist',
        'href': '/album?browse=MPREfavorite', 'play_url': 'https://music.youtube.com/browse/MPREfavorite'}]})
    expect(dialog.locator('#music-catalog-count')).to_have_text('2')
    expect(dialog.locator('[data-catalog-kind="album"]')).to_be_visible()
    expect(dialog.get_by_text('Searching YouTube Music…', exact=True)).to_be_hidden()
    assert not errors


@pytest.mark.parametrize('width', [1440, 1920])
def test_overlay_adds_without_closing_and_restores_keyboard_focus(music_app, store, page, width):
    base, catalog = music_app
    store.tracks.upsert_track('local-song', 'A local favorite', 'Favorite artist', duration_s=180)
    errors = prepare_page(page, width)
    page.goto(base + '/playlists')
    expect(page.locator('.global-now-playing')).to_contain_text('Still playing')
    page.evaluate('window.searchPlayer = document.querySelector(".global-now-playing")')
    page.locator('.omni-input').focus()
    page.keyboard.press('Control+k')
    dialog = page.locator('#music-search')
    field = page.get_by_role('searchbox', name='Search music')
    expect(field).to_be_focused()
    field.fill('favorite')
    expect(dialog.locator('.omni-row', has_text='A local favorite')).to_be_visible()
    expect(dialog.locator('#music-library-count')).to_have_text('2')
    # The hidden catalog panel is searched before its tab is selected.
    expect(dialog.locator('#music-catalog-count')).to_be_visible()
    expect(dialog.locator('#music-catalog-count')).to_have_text('1')
    expect(dialog.get_by_role('tab', name='My library', exact=True)).to_have_accessible_description('2 results')
    dialog.get_by_role('tab', name='My library', exact=True).focus()
    page.keyboard.press('Control+k')
    expect(field).to_be_focused()
    expect(field).to_have_value('favorite')
    dialog.get_by_role('tab', name='YouTube Music', exact=True).click()
    row = dialog.locator('[data-video-id="exact-song"]')
    expect(row).to_contain_text('Favorite album · 3:42')
    play = row.get_by_role('link', name='Play A favorite song', exact=True)
    field.hover()
    expect(play).to_have_css('opacity', '0')
    row.locator('.pl-cover').hover()
    expect(play).to_have_css('opacity', '1')
    page.route('**/play', lambda route: route.fulfill(json={'ok': True}))
    with page.expect_request('**/play') as request:
        play.click()
    assert request.value.post_data_json == {'url': 'https://music.youtube.com/watch?v=exact-song'}
    expect(dialog).to_be_visible()
    assert not store.tracks.library_identity_key_for_video('exact-song')
    row.get_by_role('button', name='Add to library:', exact=False).click()
    expect(row.get_by_role('button', name='Remove from library:', exact=False)).to_have_attribute('aria-pressed', 'true')
    assert row.locator('.music-search-add').evaluate('el => el.getBoundingClientRect().width') == 42
    assert row.locator('.music-search-add svg').evaluate('el => el.getBoundingClientRect().width') == 22
    page.screenshot(path=f'/tmp/tuneconsole-1239-catalog-{width}.png', full_page=True)
    dialog.get_by_role('tab', name='My library', exact=True).click()
    local = dialog.locator('[data-library-video="exact-song"]')
    expect(local).to_be_visible()
    expect(dialog.locator('#music-library-count')).to_have_text('4')
    expect(local).to_contain_text('A favorite song')
    expect(local.get_by_role('button', name='Add to playlist: A favorite song')).to_be_visible()
    expect(dialog).to_be_visible()
    assert catalog.writes == [['private-add-token']] and catalog.rated == []
    if width == 1440:
        dialog.get_by_role('tab', name='YouTube Music', exact=True).click()
        row.get_by_role('button', name='Remove from library:', exact=False).click()
        expect(row.locator('.music-search-add')).to_have_attribute('aria-pressed', 'false')
        dialog.get_by_role('tab', name='My library', exact=True).click()
        expect(local).to_have_count(0)
        expect(dialog.locator('#music-library-count')).to_have_text('2')
        dialog.get_by_role('tab', name='YouTube Music', exact=True).click()
        row.get_by_role('button', name='Add to library:', exact=False).click()
        expect(row.locator('.music-search-add')).to_have_attribute('aria-pressed', 'true')
        dialog.get_by_role('tab', name='My library', exact=True).click()
        expect(local).to_be_visible()
        expect(dialog.locator('#music-library-count')).to_have_text('4')
        assert catalog.writes == [['private-add-token'], ['private-remove-token'], ['private-add-token']]
    assert page.evaluate('searchPlayer === document.querySelector(".global-now-playing")')
    expect(page.locator('.global-now-playing')).to_contain_text('Still playing')
    assert dialog.evaluate('el => el.getBoundingClientRect().width > 900')
    field.focus()
    page.keyboard.press('ArrowDown')
    assert page.evaluate('document.activeElement.matches("#music-search [data-music-results] a")')
    page.screenshot(path=f'/tmp/tuneconsole-1239-search-{width}.png', full_page=True)
    page.keyboard.press('Escape')
    expect(dialog).to_be_hidden()
    expect(page.locator('.omni-input')).to_be_focused()
    page.keyboard.press('Control+k')
    expect(field).to_have_value('favorite')
    assert not errors


@pytest.mark.parametrize('outcome', ['saved', 'uncertain', 'unavailable'])
def test_song_bookmark_uses_the_query_that_found_it(music_app, page, outcome):
    base, catalog = music_app
    search = catalog.search
    offered = True

    def search_offered_songs(query, **options):
        return search(query, **options) if offered and query in ('favorite', 'favorite song') else []

    catalog.search = search_offered_songs
    catalog.uncertain = outcome == 'uncertain'
    errors = prepare_page(page, 1440)
    page.goto(base + '/playlists')
    page.keyboard.press('Control+k')
    dialog = page.locator('#music-search')
    field = dialog.get_by_role('searchbox', name='Search music')
    field.fill('favorite')
    dialog.get_by_role('tab', name='YouTube Music', exact=True).click()
    bookmark = dialog.locator('[data-video-id="exact-song"] .music-search-add')
    expect(bookmark).to_have_attribute('aria-label', 'Add to library: A favorite song')

    # Trigger the click before the debounced search replaces the visible row.
    offered = outcome != 'unavailable'
    with page.expect_response('**/search/songs/save') as response:
        field.evaluate('''input => {
          input.value = 'another song';
          input.dispatchEvent(new Event('input', {bubbles: true}));
          document.querySelector('[data-video-id="exact-song"] .music-search-add').click();
        }''')
    assert response.value.request.post_data_json['query'] == 'favorite'
    assert response.value.json()['state'] == ('pending' if outcome == 'uncertain' else outcome)
    expect(dialog.locator('#music-catalog-count')).to_have_text('0')
    offered = True
    field.fill('favorite song' if outcome == 'unavailable' else 'favorite')

    if outcome == 'uncertain':
        expect(bookmark).to_have_attribute('aria-label', 'Check library change: A favorite song')
        catalog.saved.add('exact-song')
        with page.expect_request('**/search/songs/save') as check:
            bookmark.click()
        assert check.value.post_data_json['check'] is True
    elif outcome == 'unavailable':
        expect(bookmark).to_have_attribute('aria-label', 'Add to library: A favorite song')
        assert not catalog.writes
        bookmark.click()
    expect(bookmark).to_have_attribute('aria-label', 'Remove from library: A favorite song')
    assert catalog.writes == [['private-add-token']]
    assert not errors


def test_song_only_onboarding_retains_picks_through_reload_and_live_start(music_app, store, page):
    from tests.browser.welcome_support import dismiss_welcome
    base, catalog = music_app
    errors = prepare_page(page, 1440)
    page.goto(base)
    dismiss_welcome(page)
    page.locator('#artist-start-search').click()
    page.get_by_role('searchbox', name='Search music').fill('favorite')
    expect(page.locator('#music-catalog-count')).to_have_text('1')
    page.locator('[data-video-id="exact-song"]').get_by_role('button', name='Add to library:', exact=False).click()
    expect(page.locator('[data-video-id="exact-song"] .music-search-add')).to_have_attribute('aria-pressed', 'true')
    page.get_by_role('button', name='Back to my picks', exact=True).click()
    expect(page.locator('.artist-start-song')).to_contain_text('A favorite song')
    page.locator('.artist-start-song .pl-cover').hover()
    expect(page.locator('.artist-start-song .pl-play')).to_have_css('opacity', '1')
    expect(page.get_by_role('button', name='Find my first set', exact=True)).to_be_visible()
    assert artist_start.required(store)
    assert not store.discovery.starter_artists()
    page.reload()
    expect(page.locator('.artist-start-song')).to_contain_text('A favorite song')
    page.screenshot(path='/tmp/tuneconsole-1239-onboarding-songs.png', full_page=True)
    page.get_by_role('button', name='Find my first set', exact=True).click()
    expect(page.get_by_role('button', name='Play my first set', exact=True)).to_be_visible()
    assert page.locator('#artist-first-listen .gen-row').get_attribute('data-vid') == 'exact-song'
    page.get_by_role('button', name='Play my first set', exact=True).click()
    expect(page.get_by_role('heading', name='Your music is on its way.')).to_be_visible()
    store.history.record_play_event(1, store.tracks.library_identity_key_for_video('exact-song'),
                                    'exact-song', 1001., playlist_ytm_id='PL_NEW_0')
    playing = {'connected': True, 'now_playing': {'video_id': 'exact-song', 'title': 'A favorite song',
               'artist': 'Favorite artist', 'paused': False, 'playlist_ytm_id': 'PL_NEW_0'}}
    page.evaluate('(data) => window.dispatchEvent(new CustomEvent("bridge-status", {detail: data}))', playing)
    expect(page.locator('[data-onboarding-artists]')).to_have_count(0)
    assert not artist_start.required(store)
    assert not catalog.rated
    assert not errors


@pytest.mark.parametrize('source', ['library', 'catalog'])
def test_playlist_control_uses_shared_dialog_and_preserves_search(music_app, store, page, source):
    base, catalog = music_app
    playlist = store.playlists.upsert_playlist(1, 'DEST', 'Evening mix', 0, '', 999.)
    if source == 'library':
        store.tracks.upsert_track('exact-song', 'A favorite song', 'Favorite artist')
    errors = prepare_page(page, 1440)
    page.goto(base + '/playlists')
    page.locator('.omni-input').click()
    field = page.get_by_role('searchbox', name='Search music')
    field.fill('favorite artist song')
    if source == 'catalog':
        page.get_by_role('tab', name='YouTube Music', exact=True).click()
    search = page.locator('#music-search')
    search.get_by_role('button', name='Add to playlist: A favorite song', exact=True).click()
    dialog = page.locator('.song-playlist-dialog')
    expect(dialog).to_be_visible()
    dialog.get_by_role('radio').check()
    dialog.get_by_role('button', name='Add song', exact=True).click()
    expect(dialog).to_contain_text('Song added.')
    assert catalog.added == [('DEST', ['exact-song'])]
    assert [row['video_id'] for row in store.charts.playlist_tracks_detail(playlist)] == ['exact-song']
    if source == 'catalog':
        assert store.charts.playlist_tracks_detail(playlist)[0]['album_browse'] == 'MPREfavorite'
    dialog.get_by_role('button', name='Done', exact=True).click()
    expect(search).to_be_visible()
    expect(field).to_have_value('favorite artist song')
    if source == 'catalog':
        bookmark = search.locator('[data-video-id="exact-song"] .music-search-add')
        expect(bookmark).to_have_attribute('aria-pressed', 'true')
        expect(bookmark).to_have_attribute('aria-label', 'In library: A favorite song')
        expect(bookmark).to_be_disabled()
    page.get_by_role('tab', name='My library', exact=True).click()
    expect(search.locator('[data-library-video="exact-song"]')).to_be_visible()
    assert not catalog.rated and not errors


def test_library_results_survive_catalog_failure(music_app, store, page):
    base, _ = music_app
    store.tracks.upsert_track('local-song', 'A local favorite', 'Favorite artist')
    errors = prepare_page(page, 1440)
    page.route('**/search/catalog?*', lambda route: route.abort())
    page.goto(base + '/playlists')
    page.locator('.omni-input').click()
    page.locator('#music-search-query').fill('favorite')
    expect(page.locator('#music-search .omni-row', has_text='A local favorite')).to_be_visible()
    expect(page.locator('#music-library-count')).to_have_text('2')
    page.get_by_role('tab', name='YouTube Music', exact=True).click()
    expect(page.get_by_role('button', name='Search YouTube Music again')).to_be_visible()
    expect(page.locator('#music-catalog-count')).to_be_hidden()
    page.unroute('**/search/catalog?*')
    page.get_by_role('button', name='Search YouTube Music again').click()
    expect(page.locator('[data-video-id="exact-song"]')).to_be_visible()
    expect(page.locator('#music-catalog-count')).to_have_text('1')
    page.route('**/search/catalog?*', lambda route: route.fulfill(json={'songs': []}))
    page.locator('#music-search-query').fill('no matching music')
    expect(page.locator('#music-library-count')).to_be_visible()
    expect(page.locator('#music-library-count')).to_have_text('0')
    expect(page.locator('#music-catalog-count')).to_be_visible()
    expect(page.locator('#music-catalog-count')).to_have_text('0')
    page.locator('#music-search-query').fill('a')
    expect(page.locator('#music-library-count')).to_be_hidden()
    expect(page.locator('#music-catalog-count')).to_be_hidden()
    assert not errors


@pytest.mark.parametrize('width', [1440, 1920])
@pytest.mark.parametrize('music_app', [EntityCatalog], indirect=True)
def test_existing_membership_and_sticky_source_tabs(music_app, store, page, width):
    base, catalog = music_app
    original = catalog.search

    def search(query, filter='songs', **kwargs):
        rows = original(query, filter=filter, **kwargs)
        if filter is None:
            return rows + [dict(resultType='album', browseId='MPREcached', title='Cached favorite album')]
        return rows + [{**rows[0], 'videoId': f'other-song-{i}', 'title': f'Favorite song {i}'} for i in range(19)]

    catalog.search = search
    track = store.tracks.upsert_track('exact-song', 'A favorite song', 'Favorite artist')
    playlist = store.playlists.upsert_playlist(1, 'LM', 'Liked Music', 1, '', 999.)
    store.playlists.set_playlist_tracks(playlist, [track])
    store.collection.add_saved_album({'browse': 'MPREfavorite', 'title': 'Favorite album',
                                      'artist': 'Favorite artist'}, account_id=1)
    store.collection.ensure_album_identity('MPREcached')
    errors = prepare_page(page, width)
    page.goto(base + '/playlists')
    page.keyboard.press('Control+k')
    field = page.get_by_role('searchbox', name='Search music')
    field.fill('favorite')
    dialog = page.locator('#music-search')
    dialog.get_by_role('tab', name='YouTube Music', exact=True).click()
    expect(dialog.locator('#music-catalog-count')).to_have_text('24')
    song = dialog.locator('[data-video-id="exact-song"] .music-search-add')
    album = dialog.get_by_role('button', name='In library: Favorite album', exact=True)
    expect(song).to_have_attribute('aria-pressed', 'true')
    expect(song).to_have_attribute('aria-label', 'In library: A favorite song')
    expect(song).to_be_disabled()
    expect(album).to_have_attribute('aria-pressed', 'true')
    expect(album).to_be_disabled()
    expect(dialog.get_by_role('button', name='Add to library: Cached favorite album', exact=True)).to_have_attribute(
        'aria-pressed', 'false')
    page.screenshot(path=f'/tmp/tuneconsole-1239-membership-{width}.png', full_page=True)
    dialog.evaluate('el => { el.scrollTop = el.scrollHeight; }')
    tabs = dialog.get_by_role('tablist', name='Search source')
    expect(tabs).to_be_in_viewport()
    assert dialog.evaluate('el => el.scrollTop > 1000')
    assert tabs.evaluate('el => el.getBoundingClientRect().top') >= dialog.evaluate('el => el.getBoundingClientRect().top')
    page.screenshot(path=f'/tmp/tuneconsole-1239-sticky-tabs-{width}.png', full_page=True)
    dialog.get_by_role('tab', name='My library', exact=True).click()
    expect(dialog.locator('#music-library-results')).to_be_visible()
    expect(dialog.locator('[data-library-video="exact-song"]')).to_be_visible()
    dialog.get_by_role('tab', name='YouTube Music', exact=True).click()
    expect(song).to_have_attribute('aria-pressed', 'true')
    expect(field).to_have_value('favorite')
    assert not catalog.writes and not catalog.entity_writes and not catalog.rated and not errors


def test_both_sources_share_entity_artwork_and_playback(music_app, store, page):
    base, catalog = music_app
    original = catalog.search

    def mixed_search(query, filter='songs', **kwargs):
        if filter is not None:
            return original(query, filter=filter, **kwargs)
        return [
            {'category': 'Top result', 'resultType': 'artist',
             'artists': [{'name': 'Favorite artist', 'id': 'UCfavorite'}], 'shuffleId': 'RDAOshuffle',
             'thumbnails': [{'url': 'https://example.com/artist.jpg'}]},
            {'resultType': 'album', 'title': 'Favorite album', 'browseId': 'MPREfavorite',
             'artist': 'Favorite artist', 'thumbnails': [{'url': 'https://example.com/album.jpg'}]},
            {'resultType': 'playlist', 'title': 'Favorite mix', 'browseId': 'VLPLfavorite',
             'author': 'Curator', 'thumbnails': [{'url': 'https://example.com/playlist.jpg'}]},
        ]

    catalog.search = mixed_search
    catalog.get_artist = lambda browse_id: {'name': 'Favorite artist', 'songs': {'results': []}}
    track = store.tracks.upsert_track('local-song', 'A local favorite', 'Favorite artist',
        'Favorite album', album_browse_id='MPRElocal', thumbnail='https://example.com/song.jpg',
        credits=[{'name': 'Favorite artist', 'id': 'UCfavorite'}])
    playlist = store.playlists.upsert_playlist(1, 'PLlocal', 'Favorite mix', 1, '', 999.)
    store.playlists.set_playlist_tracks(playlist, [track])
    errors = prepare_page(page, 1440)
    page.route('**/play', lambda route: route.fulfill(json={'ok': True}))
    page.route('**/album/play', lambda route: route.fulfill(status=204))
    page.goto(base + '/playlists')
    page.locator('.omni-input').focus()
    page.keyboard.press('Meta+k')
    field = page.get_by_role('searchbox', name='Search music')
    expect(field).to_be_focused()
    field.fill('favorite')
    search = page.locator('#music-search')
    expect(search.locator('#music-library-count')).to_have_text('4')
    expect(search.locator('#music-catalog-count')).to_have_text('4')
    for source, urls in [
        ('library', ['watch?v=local-song', 'browse/UCfavorite', 'browse/MPRElocal', 'playlist?list=PLlocal']),
        ('catalog', ['watch?v=exact-song', 'playlist?list=RDAOshuffle', 'browse/MPREfavorite', 'playlist?list=PLfavorite']),
    ]:
        search.get_by_role('tab', name='My library' if source == 'library' else 'YouTube Music', exact=True).click()
        panel = search.locator('#music-library-results' if source == 'library' else '#music-catalog-results')
        expect(panel).to_be_visible()
        expect(panel.locator('.pl-cover')).to_have_count(4)
        if source == 'library':
            assert panel.locator('.omni-row').evaluate_all(
                'rows => rows.map(row => row.dataset.libraryKind || "song")') == ['artist', 'song', 'album', 'playlist']
        else:
            assert panel.locator('.music-search-entity, .music-search-song').evaluate_all(
                'rows => rows.map(row => row.dataset.catalogKind || "song")') == ['artist', 'song', 'album', 'playlist']
            artist_row = panel.locator('[data-catalog-kind="artist"]')
            expect(artist_row.get_by_role('link', name='Favorite artist Artist', exact=True)).to_have_attribute(
                'href', '/artist?browse=UCfavorite')
            expect(artist_row.get_by_role('button', name='Add to library: Favorite artist', exact=True)).to_be_visible()
        assert panel.locator('a a').count() == 0
        for url in urls:
            play = panel.locator(f'a.pl-play[href="https://music.youtube.com/{url}"]')
            cover = play.locator('..')
            assert cover.evaluate('el => [el.clientWidth, el.clientHeight]') == [58, 58]
            field.hover()
            expect(play).to_have_css('opacity', '0')
            cover.hover()
            expect(play).to_have_css('opacity', '1')
            is_album = url.startswith('browse/MPRE')
            with page.expect_request('**/album/play' if is_album else '**/play') as request:
                play.click()
            if is_album:
                assert parse_qs(request.value.post_data)['browse_id'] == [url.removeprefix('browse/')]
            else:
                assert request.value.post_data_json == {'url': 'https://music.youtube.com/' + url}
            expect(search).to_be_visible()
            expect(field).to_have_value('favorite')
        for width in (1920, 1440):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert panel.locator('.pl-cover').evaluate_all(
                'covers => covers.map(el => [el.clientWidth, el.clientHeight])') == [[58, 58]] * 4
            page.screenshot(path=f'/tmp/tuneconsole-1239-{source}-sections-{width}.png', full_page=True)
    assert len(search.locator('#music-library-results .omni-section').all()) == 4
    assert not catalog.writes and not catalog.rated
    page.evaluate('openMusicSearch({onboarding: true})')
    choose = search.get_by_role('button', name='Choose Favorite artist', exact=True)
    expect(choose).to_be_visible()
    choose.click()
    expect(search.get_by_role('button', name='Remove Favorite artist', exact=True)).to_have_attribute('aria-pressed', 'true')
    assert [row['browse_id'] for row in store.discovery.starter_artists()] == ['UCfavorite']
    expect(field).to_have_value('favorite')
    assert not errors


@pytest.mark.parametrize('music_app', [EntityCatalog], indirect=True)
def test_catalog_entities_save_and_recover_in_their_existing_controls(music_app, store, page):
    base, catalog = music_app
    errors = prepare_page(page, 1440)
    page.goto(base + '/playlists')
    page.keyboard.press('Control+k')
    field = page.get_by_role('searchbox', name='Search music')
    field.fill('favorite')
    page.get_by_role('tab', name='YouTube Music', exact=True).click()
    panel = page.locator('#music-catalog-results')
    expect(page.locator('#music-catalog-count')).to_have_text('4')
    artist = panel.locator('[data-catalog-kind="artist"] .music-search-add')
    catalog.outcome = 'uncertain'
    artist.click()
    expect(artist).to_have_attribute('aria-label', 'Check library change: Favorite artist')
    artist.click()
    expect(artist).to_have_attribute('aria-label', 'Check library change: Favorite artist')
    assert len(catalog.entity_writes) == 1
    catalog.members.add('artist')
    artist.click()
    expect(artist).to_have_attribute('aria-pressed', 'true')
    expect(artist).to_be_disabled()
    album = panel.locator('[data-catalog-kind="album"] .music-search-add')
    catalog.outcome = 'not-dispatched'
    album.click()
    expect(album).to_have_attribute('aria-label', 'Add to library: Favorite album')
    catalog.outcome = 'confirmed'
    album.click()
    expect(album).to_have_attribute('aria-pressed', 'true')
    playlist = panel.locator('[data-catalog-kind="playlist"] .music-search-add')
    playlist.click()
    expect(playlist).to_have_attribute('aria-pressed', 'true')
    assert [kind for kind, target in catalog.entity_writes] == ['artist', 'album', 'playlist']
    assert not catalog.writes and not catalog.rated
    expect(field).to_have_value('favorite')
    expect(page.locator('#music-search')).to_be_visible()
    expect(page.locator('.global-now-playing')).to_contain_text('Still playing')
    page.screenshot(path='/tmp/tuneconsole-1239-entity-saves.png', full_page=True)
    page.get_by_role('tab', name='My library', exact=True).click()
    expect(page.locator('#music-search .omni-row', has_text='Favorite playlist')).to_be_visible()
    expect(page.locator('#music-search [data-library-video="exact-song"]')).to_be_visible()
    assert not errors


def test_artist_song_and_playlist_queries_keep_the_same_section_order_in_both_tabs(music_app, store, page):
    base, catalog = music_app
    track = store.tracks.upsert_track('shpongle-song', 'Favorite song', 'Shpongle', 'Release',
                                    album_browse_id='MPRErelease', credits=[{'name': 'Shpongle', 'id': 'UCshpongle'}])
    playlist = store.playlists.upsert_playlist(1, 'PLmix', 'Evening mix', 1, '', 999.)
    store.playlists.set_playlist_tracks(playlist, [track])

    def search(query, filter='songs', **kwargs):
        if filter is not None:
            return [] if query == 'evening mix' else [dict(videoId='shpongle-song', title='Favorite song',
                artists=[{'name': 'Shpongle', 'id': 'UCshpongle'}])]
        if query == 'favorite song':
            return []
        rows = [dict(resultType='playlist', browseId='VLPLmix', title='Evening mix')]
        if query == 'shpongle':
            rows += [dict(resultType='artist', browseId='UCshpongle', artist='Shpongle'),
                     dict(resultType='album', browseId='MPRErelease', title='Release', artist='Shpongle')]
        return rows

    catalog.search = search
    errors = prepare_page(page, 1440)
    page.goto(base + '/playlists')
    page.keyboard.press('Control+k')
    dialog = page.locator('#music-search')
    for query, expected in [('shpongle', ['artist', 'song', 'album', 'playlist']),
                            ('favorite song', ['song']), ('evening mix', ['playlist'])]:
        page.get_by_role('searchbox', name='Search music').fill(query)
        expect(dialog.locator('#music-library-count')).to_have_text(str(len(expected)))
        expect(dialog.locator('#music-catalog-count')).to_have_text(str(len(expected)))
        dialog.get_by_role('tab', name='My library', exact=True).click()
        library = dialog.locator('#music-library-results')
        expect(library).to_be_visible()
        if expected == ['playlist']:
            expect(library.locator('[data-library-kind="playlist"]')).to_have_count(1)
        assert library.locator('.omni-row').evaluate_all(
            'rows => rows.map(row => row.dataset.libraryKind || "song")') == expected
        dialog.get_by_role('tab', name='YouTube Music', exact=True).click()
        native = dialog.locator('#music-catalog-results')
        expect(native).to_be_visible()
        if expected == ['playlist']:
            expect(native.locator('[data-catalog-kind="playlist"]')).to_have_count(1)
        assert native.locator('.music-search-entity, .music-search-song').evaluate_all(
            'rows => rows.map(row => row.dataset.catalogKind || "song")') == expected
    assert not errors
