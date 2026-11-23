"""Artist navigation must keep homonymous credits separate across surfaces."""
from html.parser import HTMLParser
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.web.app import create_app


def seed_artists(store):
    existing = store.accounts.get_accounts()
    account = existing[0].id if existing else store.accounts.upsert_account('Main', None, True)
    playlist = store.playlists.upsert_playlist(account, 'PL_same_names', 'Same names', 2, 'h', 0)
    tracks = []
    for video, title, album, browse, channel in (
            ('matador', 'Kingswing', 'Velvet Revolver', 'B_matador', 'UC_matador'),
            ('other', 'Other song', 'Other release', 'B_other', 'UC_other')):
        tracks.append(store.tracks.upsert_track(video, title, 'Matador', album, 180,
            album_browse_id=browse, artist_browse_id=channel))
        store.collection.add_saved_album({'browse': browse, 'title': album, 'artist': 'Matador'})
    store.playlists.set_playlist_tracks(playlist, tracks)
    store.history.record_history_plays(account, 1000, ['kingswing|matador', 'other song|matador'], source_timezone='UTC')
    ids = [store.artists.track_credits(track)[0]['artist_id'] for track in tracks]
    assert ids[0] != ids[1] and store.artists.resolve('Matador') is None
    return account, playlist, ids


def artist_hrefs(response):
    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.hrefs = []

        def handle_starttag(self, tag, attrs):
            href = dict(attrs).get('href') or ''
            if tag == 'a' and href.startswith('/artist?'):
                self.hrefs.append(href)

    parser = Links()
    parser.feed(response.text)
    return parser.hrefs


def test_album_track_playlist_indexes_and_charts_link_the_credited_identity(store):
    account, playlist, (matador, other) = seed_artists(store)
    calls = []

    class Catalog(FakeClient):
        def search(self, *args, **kwargs):
            raise AssertionError('Artist navigation must not search by name')

        def get_artist(self, browse):
            calls.append(browse)
            return {'name': 'Matador', 'description': 'Correct biography for ' + browse}

    client = TestClient(create_app(store, lambda: {account: Catalog()}, background_work=False),
                        base_url='http://127.0.0.1')
    expected = {f'/artist?artist_id={matador}', f'/artist?artist_id={other}'}
    for path in ('/artists', '/albums', '/albums?view=saved', f'/playlist/{playlist}', '/charts?view=songs',
                 '/charts?view=artists'):
        response = client.get(path)
        assert response.status_code == 200, path
        assert set(artist_hrefs(response)) == expected, path
    for path in ('/album?browse=B_matador', '/track/matador'):
        response = client.get(path)
        assert response.status_code == 200
        assert set(artist_hrefs(response)) == {f'/artist?artist_id={matador}'}
    response = client.get('/artist', params={'artist_id': matador, 'name': 'Wrong label', 'browse': 'UC_other'})
    assert response.status_code == 200
    assert 'Correct biography for UC_matador' in response.text
    assert [song['title'] for song in response.context['songs']] == ['Kingswing']
    assert calls == ['UC_matador']
    assert client.get('/artist?name=Matador').status_code == 404
    assert client.get('/artist?artist_id=999999&name=Matador&browse=UC_matador').status_code == 404
    assert calls == ['UC_matador']


def test_provisional_and_unknown_provider_artists_never_acquire_a_name_match(store):
    account = store.accounts.upsert_account('Main', None, True)
    track = store.tracks.upsert_track('local', 'Local song', 'Same', '', 180)
    playlist = store.playlists.upsert_playlist(account, 'PL', 'Mix', 1, 'h', 0)
    store.playlists.set_playlist_tracks(playlist, [track])
    artist = store.artists.track_credits(track)[0]['artist_id']
    calls = []

    class Catalog(FakeClient):
        def search(self, *args, **kwargs):
            raise AssertionError('A provisional name is not a provider identity')

        def get_artist(self, browse):
            calls.append(browse)
            return {'name': 'Same', 'description': 'Different artist'}

    client = TestClient(create_app(store, lambda: {account: Catalog()}, background_work=False),
                        base_url='http://127.0.0.1')
    response = client.get(f'/artist?artist_id={artist}')
    assert response.status_code == 200 and response.context['info'] is None
    assert [song['title'] for song in response.context['songs']] == ['Local song']
    assert calls == []
    response = client.get('/artist?browse=UC_unowned')
    assert response.status_code == 200 and response.context['artist'] == 'Same'
    assert response.context['songs'] == [] and response.context['collection'] == []
    assert calls == ['UC_unowned']
    assert store.artists.navigation(name='Same', video='unknown-video') == []
    assert store.artists.navigation(name='Same', album='unknown-album') == []
    assert store.artists.navigation(name='Same') == []


def test_collaboration_and_live_provider_credits_keep_each_artist_identity(store):
    account, playlist, (matador, other) = seed_artists(store)
    collab = store.tracks.upsert_track('collab', 'Collaboration', 'Matador & Guest', '', 180,
        credits=[{'name': 'Matador', 'id': 'UC_matador'}, {'name': 'Guest', 'id': 'UC_guest'}])
    store.playlists.set_playlist_tracks(playlist, [collab])
    guest = store.artists.track_credits(collab)[1]['artist_id']
    client = TestClient(create_app(store, lambda: {}, background_work=False), base_url='http://127.0.0.1')
    assert artist_hrefs(client.get(f'/playlist/{playlist}')) == [
        f'/artist?artist_id={matador}', f'/artist?artist_id={guest}']
    metadata = client.get('/songs/metadata?video_id=collab').json()['metadata']
    assert [(row['name'], row['href']) for row in metadata['artist_links']] == [
        ('Matador', f'/artist?artist_id={matador}'), ('Guest', f'/artist?artist_id={guest}')]
    from yt_playlist.web.artist_links import navigation, radio_navigation
    links = navigation(store.artists)({}, {'artist': 'Matador', 'artists': [
        {'name': 'Matador', 'id': 'UC_external'}]}, album=True)
    assert links == [{'name': 'Matador', 'artist_id': None, 'browse': 'UC_external',
                      'href': '/artist?browse=UC_external'}]
    state = radio_navigation({'directions': [{'tracks': [{'video_id': 'collab', 'artist': 'Matador & Guest'}]}]}, store.artists)
    assert [row['href'] for row in state['directions'][0]['tracks'][0]['artist_links']] == [
        f'/artist?artist_id={matador}', f'/artist?artist_id={guest}']


def test_artist_tickers_keep_same_name_identities_separate(store):
    _, _, (first, second) = seed_artists(store)
    expected = {f'@artist:{first}': 1, f'@artist:{second}': 1}
    assert store.charts.corpus_distribution('artist') == expected
    assert store.charts.listen_distribution('artist') == expected
    assert store.charts.artist_labels()[f'@artist:{first}'] == 'Matador'
    assert store.charts.artist_labels()[f'@artist:{second}'] == 'Matador'
