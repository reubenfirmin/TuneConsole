"""Track actions and navigation through the desktop detail page."""
import pytest
from playwright.sync_api import expect

from tests.browser.test_song_actions_browser import song_app  # noqa: F401 - shared fixture


def test_native_counterpart_highlights_planned_row_without_fuzzy_version_matching(song_app, page):
    store = song_app['store']
    planned = store.tracks.upsert_track('otg8rxwvfwk', 'El ruso', 'Daymé Arocena', '', 153)
    actual = store.tracks.upsert_track('aekyGjGSsh4', 'El Ruso (Daymé’s Version)', 'Daymé Arocena', '', 151)
    store.playlists.set_playlist_tracks(song_app['source'], [planned, actual])
    status = {'connected': False, 'now_playing': None}
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    original = page.locator('tr[data-vid="otg8rxwvfwk"]')
    counterpart = page.locator('tr[data-vid="aekyGjGSsh4"]')

    def playing(video, item=None):
        status.update(connected=True, now_playing={
            'video_id': video, 'title': 'El Ruso (Daymé’s Version)', 'artist': 'Daymé Arocena',
            'playlist_ytm_id': 'SOURCE', 'queue_item': item})
        page.evaluate('window.playbackStatus.refresh()')
    playing('aekyGjGSsh4', {'video_id': 'otg8rxwvfwk', 'playlist': 'SOURCE', 'index': 0})
    expect(original).to_have_class('srow now-playing')
    expect(counterpart).not_to_have_class('srow now-playing')
    expect(page.locator('.global-now-playing')).to_contain_text('El Ruso (Daymé’s Version)')
    # The queue position can differ from the local snapshot. The native
    # counterpart identity still identifies the planned upload.
    playing('aekyGjGSsh4', {'video_id': 'otg8rxwvfwk', 'playlist': 'SOURCE', 'index': 1})
    expect(original).to_have_class('srow now-playing')
    expect(counterpart).not_to_have_class('srow now-playing')
    playing('aekyGjGSsh4', {'video_id': 'aekyGjGSsh4', 'playlist': 'SOURCE', 'index': 0})
    expect(original).not_to_have_class('srow now-playing')
    expect(counterpart).to_have_class('srow now-playing')
    playing('aekyGjGSsh4', {'video_id': 'otg8rxwvfwk', 'playlist': 'OTHER', 'index': 0})
    expect(original).not_to_have_class('srow now-playing')
    expect(counterpart).to_have_class('srow now-playing')
    playing('unrelated')
    expect(original).not_to_have_class('srow now-playing')
    # A replacement with no local identity match cannot claim an unrelated row
    # from its endpoint position alone. Exact title/artist fallback remains.
    playing('replacement', {'video_id': 'replacement', 'playlist': 'SOURCE', 'index': 0})
    expect(original).not_to_have_class('srow now-playing')
    expect(counterpart).to_have_class('srow now-playing')
    # Sorting changes the DOM order, while each row retains its source position.
    page.get_by_role('button', name='Song', exact=True).click()
    page.get_by_role('button', name='Song', exact=True).click()
    expect(page.locator('#playlist-track-body tr').first).to_have_attribute('data-vid', 'aekyGjGSsh4')
    playing('aekyGjGSsh4', {'video_id': 'otg8rxwvfwk', 'playlist': 'SOURCE', 'index': 0})
    expect(original).to_have_class('srow now-playing')
    expect(counterpart).not_to_have_class('srow now-playing')


def test_native_queue_position_cannot_highlight_a_different_song(song_app, page):
    store = song_app['store']
    before = store.tracks.upsert_track('prophecy', 'Prophecy At 1420 MHz', 'Boards of Canada', 'Inferno', 180)
    current = store.tracks.upsert_track('want2', 'Want 2', 'Eden Aurelius', 'Plateau', 180)
    store.playlists.set_playlist_tracks(song_app['source'], [before, current])
    status = {'connected': False, 'now_playing': None}
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    for item in (None, {'video_id': 'want2', 'playlist': 'SOURCE', 'index': 0},
                 {'video_id': 'want2', 'playlist': 'SOURCE', 'index': 1},
                 {'video_id': 'want2', 'playlist': 'SOURCE', 'index': 99}):
        status.update(connected=True, now_playing={
            'video_id': 'want2', 'title': 'Want 2', 'artist': 'Eden Aurelius',
            'playlist_ytm_id': 'SOURCE', 'queue_item': item})
        page.evaluate('window.playbackStatus.refresh()')
        expect(page.locator('.global-now-playing')).to_contain_text('Want 2')
        expect(page.locator('tr.now-playing')).to_have_count(1)
        expect(page.locator('tr.now-playing')).to_have_attribute('data-vid', 'want2')
    status['now_playing'].update(video_id='unknown', title='Unknown song', artist='Unknown artist',
                                queue_item={'video_id': 'unknown', 'playlist': 'SOURCE', 'index': 0})
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.locator('tr.now-playing')).to_have_count(0)
    status['now_playing'].update(video_id='want2', title='Want 2', artist='Eden Aurelius',
                                queue_item={'video_id': 'want2', 'playlist': 'SOURCE', 'index': 0})
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.locator('tr.now-playing')).to_have_attribute('data-vid', 'want2')


def test_native_position_disambiguates_repeated_uploads_after_sorting(song_app, page):
    status = {'connected': False, 'now_playing': None}
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    # Local storage deduplicates uploads. Add a repeated rendered entry to
    # exercise the table's handling of native queues that retain repetitions.
    page.locator('tr[data-vid="owned-song"]').evaluate('''row => {
        const repeated = row.cloneNode(true);
        repeated.dataset.playlistIndex = '1';
        row.after(repeated);
    }''')
    rows = page.locator('tr[data-vid="owned-song"]')
    expect(rows).to_have_count(2)
    page.get_by_role('button', name='Song', exact=True).click()
    for video in ('owned-song', 'native-counterpart'):
        status.update(connected=True, now_playing={
            'video_id': video, 'title': 'Different version', 'artist': 'Artist & Friends',
            'playlist_ytm_id': 'SOURCE',
            'queue_item': {'video_id': 'owned-song', 'playlist': 'SOURCE', 'index': 1}})
        page.evaluate('window.playbackStatus.refresh()')
        expect(page.locator('tr.now-playing')).to_have_count(1)
        expect(page.locator('tr.now-playing')).to_have_attribute('data-playlist-index', '1')


@pytest.mark.parametrize('width', [1280])
def test_track_details_actions_and_navigation(song_app, page, width, tmp_path):
    song_app['store'].tracks.upsert_track('owned-song', 'Owned Song', 'Artist & Friends', 'Album', 180,
                                          thumbnail='/static/nag/identities.png')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 900})
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    page.get_by_role('link', name='Owned Song', exact=True).click()
    expect(page.get_by_role('heading', name='Owned Song', exact=True)).to_be_visible()
    expect(page.get_by_role('link', name='Source mix', exact=True)).to_be_visible()
    page.wait_for_function("document.querySelector('.track-detail-art').naturalWidth > 0")
    art = page.locator('.track-detail-art').bounding_box()
    hero = page.locator('.library-hero.release-hero').bounding_box()
    assert art['width'] >= hero['width'] and art['height'] >= hero['height']
    ledger = page.locator('[aria-labelledby="track-ledger-title"]')
    expect(ledger.get_by_role('heading', name='Listening history')).to_be_visible()
    like = page.get_by_role('button', name='Like', exact=True)
    dislike = page.get_by_role('button', name='Dislike', exact=True)
    expect(like).to_have_text('')
    expect(dislike).to_have_text('')
    expect(page.get_by_role('link', name='Play on YouTube Music')).to_have_text('')
    like.click()
    expect(like).to_have_attribute('aria-pressed', 'true')
    dislike.click()
    expect(dislike).to_have_attribute('aria-pressed', 'true')
    expect(like).to_have_attribute('aria-pressed', 'false')
    assert song_app['main'].rated[-2:] == [('owned-song', 'LIKE'), ('owned-song', 'DISLIKE')]
    page.get_by_role('button', name='Create playlist with song', exact=True).click()
    dialog = page.get_by_role('dialog', name='Create playlist', exact=True)
    expect(dialog.get_by_label('Playlist name')).to_be_focused()
    dialog.get_by_label('Playlist name').fill('Track favorites')
    dialog.get_by_role('button', name='Create playlist', exact=True).click()
    expect(dialog.get_by_text('Playlist created.', exact=True)).to_be_visible()
    dialog.get_by_role('button', name='Done', exact=True).click()
    expect(page.get_by_role('link', name='Track favorites', exact=True)).to_be_visible()
    page.get_by_role('button', name='Edit metadata', exact=True).click()
    edit = page.get_by_role('dialog', name='Edit Metadata', exact=True)
    edit.get_by_label('Title', exact=True).fill('Corrected song')
    edit.get_by_role('button', name='Save changes', exact=True).click()
    expect(page.get_by_role('heading', name='Corrected song', exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=tmp_path / f'track-{width}.png', full_page=True, animations='disabled')
    assert not errors
