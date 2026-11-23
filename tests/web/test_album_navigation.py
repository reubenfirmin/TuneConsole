"""Missing upload metadata must not hide an exactly identified release."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app


def seed_radio_albums(store):
    tracks = []
    for video, title, artist, album, browse in (
            ('cards', 'Cards to Your Heart', 'Groove Armada',
             'Black Light 15th Anniversary (Deluxe)', 'MPRE_black_light'),
            ('missile', 'Misguided Missile', 'Manic Street Preachers',
             'Futurology (Deluxe)', 'MPRE_futurology')):
        track = store.tracks.upsert_track(video, title, artist, album, 180)
        store.albums.link(track, browse, title=album, artist=artist, source='discovery')
        tracks.append(track)
    return tracks


def test_radio_playlist_and_track_pages_share_confirmed_album_navigation(store, monkeypatch):
    tracks = seed_radio_albums(store)
    account = store.accounts.upsert_account('Main', None, True)
    playlist = store.playlists.upsert_playlist(account, 'PL_radio', 'Radio Session', 2, 'h', 0)
    store.playlists.set_playlist_tracks(playlist, tracks)
    app = create_app(store, lambda: {}, background_work=False)
    state = {'playlist_id': 'PL_radio', 'title': 'Radio Session', 'history_count': 2,
             'history': [{'video_id': 'cards'}, {'video_id': 'missile'}]}
    monkeypatch.setattr(app.state.ctx.radio, 'state', lambda: state)
    client = TestClient(app, base_url='http://127.0.0.1')
    for path in ('/radio/playlist', f'/playlist/{playlist}', '/track/cards'):
        response = client.get(path)
        assert response.status_code == 200
        assert 'href="/album?browse=MPRE_black_light"' in response.text, path
        if path != '/track/cards':
            assert 'href="/album?browse=MPRE_futurology"' in response.text, path
    assert store.tracks.track_detail('cards')['album_browse_id'] is None


def test_album_navigation_requires_one_matching_release_of_the_exact_entry(store):
    seed_radio_albums(store)
    # A same-name album elsewhere and another upload of the same song are not
    # evidence that this particular upload belongs to that release.
    store.tracks.upsert_track('copy', 'Cards to Your Heart', 'Groove Armada',
                              'Black Light 15th Anniversary (Deluxe)', 180)
    recording = store.tracks.track_detail('cards')['recording_id']
    store.tracks.associate_youtube_item('copy', recording, source='verified',
        evidence={'test': 'Confirmed alternate upload'}, now=1)
    assert store.tracks.track_detail('copy')['album_browse'] is None
    first = store.tracks.track_detail('cards')['id']
    store.albums.link(first, 'MPRE_original', title='Black Light', artist='Groove Armada')
    assert store.tracks.track_detail('cards')['album_browse'] == 'MPRE_black_light'
    store.albums.link(first, 'MPRE_ambiguous', title='Black Light 15th Anniversary (Deluxe)',
                      artist='Groove Armada')
    assert store.tracks.track_detail('cards')['album_browse'] is None
    assert store.charts.video_tracks_detail(['cards'])[0]['album_browse'] is None
    # A delivered browse ID is authoritative even with several memberships.
    store.tracks.upsert_track('cards', 'Cards to Your Heart', 'Groove Armada',
        'Black Light 15th Anniversary (Deluxe)', 180, album_browse_id='MPRE_explicit')
    assert store.tracks.track_detail('cards')['album_browse'] == 'MPRE_explicit'
    assert store.charts.video_tracks_detail(['cards'])[0]['album_browse'] == 'MPRE_explicit'
