import pytest

from yt_playlist.providers import discogs, musicbrainz, release_notes, wikipedia, album_editorial, album_facts
from yt_playlist.providers.album_editorial import lookup as editorial_lookup
from yt_playlist.rec import album_notes

ALBUM = {'browse_id': 'record', 'artist': 'Hermanos Gutiérrez', 'title': 'Sonido Cósmico'}
MBID = '12345678-1234-1234-1234-123456789abc'


@pytest.fixture(autouse=True)
def healthy_providers(monkeypatch):
    monkeypatch.setattr(musicbrainz, 'tripped', lambda: False)
    monkeypatch.setattr(discogs, 'tripped', lambda: False)
    monkeypatch.setattr(album_editorial, 'lookup', lambda *args: None)
    monkeypatch.setattr(album_facts, 'from_deezer', lambda *args: (None, {'provider': 'deezer_album', 'status': 'miss'}))
    monkeypatch.setattr(album_facts, 'from_lastfm', lambda *args: (None, {'provider': 'lastfm_album', 'status': 'not_configured'}))


def release():
    return {'id': MBID, 'title': ALBUM['title'],
            'artist-credit': [{'name': ALBUM['artist'], 'artist': {'name': ALBUM['artist']}}],
            'relations': [
                {'type': 'review', 'url': {'resource': 'https://pitchfork.com/reviews/albums/sonido-cosmico/'}},
                {'type': 'discogs', 'url': {'resource': 'https://www.discogs.com/master/123'}}]}


def test_release_rating_requires_verified_artist_and_title_and_keeps_its_scale(store, monkeypatch):
    monkeypatch.setattr(musicbrainz, '_get', lambda path, params, **kw:
                        {'release-groups': [release()]} if path == 'release-group' else release())
    monkeypatch.setattr(discogs, 'token', lambda store: None)
    public = {'title': ALBUM['title'], 'artists': [{'name': ALBUM['artist']}],
              'community': {'rating': {'average': 4.4, 'count': 19}},
              'labels': [{'name': 'Easy Eye Sound'}], 'tracklist': [{'title': 'One'}]}
    monkeypatch.setattr(discogs, '_get', lambda path, params, tok, **kw:
                        {'main_release': 456} if path.startswith('masters') else public)
    notes = release_notes.fetch(ALBUM, store)
    assert notes['rating'] == {'value': 4.4, 'count': 19, 'scale': 5,
                              'source': 'Discogs community', 'url': 'https://www.discogs.com/release/456'}
    assert any(link['label'] == 'Pitchfork review' for link in notes['links'])
    assert notes['label'] == 'Easy Eye Sound' and notes['track_count'] == 1
    public['artists'] = [{'name': 'Someone Else'}]
    assert release_notes.fetch(ALBUM, store)['rating'] is None
    public['artists'] = [{'name': ALBUM['artist']}]
    public['title'] = 'Sonido Cósmico (Live)'
    assert release_notes.fetch(ALBUM, store)['rating'] is None


def test_wrong_search_hit_is_never_opened_or_attributed(store, monkeypatch):
    calls = []
    wrong = {**release(), 'artist-credit': [{'name': 'Someone else'}]}
    def lookup(path, params, **kwargs):
        calls.append(path)
        return {'release-groups': [wrong]}
    monkeypatch.setattr(musicbrainz, '_get', lookup)
    monkeypatch.setattr(discogs, 'token', lambda store: None)
    result = release_notes.fetch(ALBUM, store)
    assert calls == ['release-group']
    assert result['rating'] is None and not result['links']


def test_notes_worker_publishes_json_evidence_and_preserves_last_good_on_outage(store, monkeypatch):
    monkeypatch.setattr(wikipedia, 'fetch_summary', lambda *args: {
        'title': ALBUM['artist'], 'extract': 'A Swiss instrumental duo.',
        'url': 'https://en.wikipedia.org/wiki/Hermanos_Gutierrez'})
    rating = {'value': 4, 'scale': 5, 'count': 20, 'source': 'Discogs community',
              'url': 'https://www.discogs.com/release/456'}
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {
        'rating': rating, 'links': [], 'label': 'Label', 'track_count': 12,
        'evidence': [{'provider': 'discogs', 'id': '456'}], 'retry': False})
    assert album_notes.prepare(store, 100, [ALBUM]) is False
    note = album_notes.read(store, ALBUM)
    assert note['rating'] == rating and 'artist_note' not in note
    raw = store.rec_ledger.evidence().get('artifact', note['evidence'])
    assert raw['sources'] == [{'provider': 'discogs', 'id': '456'},
                              {'provider': 'deezer_album', 'status': 'miss'},
                              {'provider': 'lastfm_album', 'status': 'not_configured'},
                              {'provider': 'youtube_album', 'status': 'not_connected'}]
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {
        'rating': None, 'links': [], 'label': None, 'track_count': None, 'evidence': [], 'retry': True})
    album_notes.prepare(store, 100 + 8 * 86400, [ALBUM])
    after = album_notes.read(store, ALBUM)
    assert after['rating'] == rating and after['label'] == 'Label'
    assert after['evidence'] == note['evidence']
    assert len(store.rec_ledger.evidence().records('artifact')) == 1
    assert not store.rec_proposals.get_proposals('album_note_requests')


def test_old_cached_wikipedia_identity_and_unsafe_urls_are_not_shown(store):
    store.wiki.put('artist:hermanos gutiérrez', 'artist', ALBUM['artist'], {
        'title': 'Someone Else', 'extract': 'Unrelated biography',
        'url': 'https://en.wikipedia.org/wiki/Someone_Else'}, 1)
    assert 'artist_note' not in album_notes.read(store, ALBUM)
    assert release_notes.public_url('javascript:alert(1)') is None
    assert release_notes.public_url('https://user:secret@example.com/') is None


def test_album_research_checks_both_identities_and_editions():
    entry = editorial_lookup(ALBUM)
    assert entry['artwork']['kind'] == 'performance'
    assert editorial_lookup({**ALBUM, 'artist': 'Someone Else'}) is None
    assert editorial_lookup({**ALBUM, 'title': 'Sonido Cósmico (Live)'}) is None
    entry['artwork']['url'] = 'Mutated'
    assert editorial_lookup(ALBUM)['artwork']['url'] != 'Mutated'


def test_public_release_copy_is_bounded_attributed_and_kept_as_evidence(store, monkeypatch):
    monkeypatch.setattr(musicbrainz, '_get', lambda path, params, **kw:
                        {'release-groups': [release()]} if path == 'release-group' else release())
    monkeypatch.setattr(discogs, 'token', lambda store: None)
    full = {'title': ALBUM['title'], 'artists': [{'name': ALBUM['artist']}],
            'notes': ' '.join(f'word{i}' for i in range(50))}
    monkeypatch.setattr(discogs, '_get', lambda path, *args, **kw:
                        {'main_release': 456} if path.startswith('masters') else full)
    result = release_notes.fetch(ALBUM, store)
    assert len(result['description']['text'].split()) == 25
    assert result['description']['source'] == 'Discogs release notes'
    assert result['description']['url'] == 'https://www.discogs.com/release/456'
    assert result['evidence'][1]['response']['notes'] == full['notes']
    monkeypatch.setattr(album_editorial, 'lookup', lambda *args: {
        'description': {'text': 'A checked album description.',
                        'source': 'Easy Eye Sound', 'url': 'https://concord.com/concord-albums/sonido-cosmico/'}})
    result = release_notes.fetch(ALBUM, store)
    assert result['description']['source'] == 'Discogs release notes'
    assert result['description']['acquisition'] == 'automatic'
    assert not any(row['provider'] == 'album_editorial' for row in result['evidence'])


def test_previous_metadata_cache_gets_prose_without_waiting_seven_days(store, monkeypatch):
    store.rec_proposals.publish_album_note(ALBUM['browse_id'], {'retry_at': 999999}, 1)
    monkeypatch.setattr(wikipedia, 'fetch_summary', lambda *args: None)
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {
        'description': {'text': 'Album prose.', 'source': 'Label notes',
                        'url': 'https://concord.com/concord-albums/sonido-cosmico/',
                        'acquisition': 'automatic'},
        'evidence': [], 'retry': False})
    album_notes.prepare(store, 2, [ALBUM])
    note = album_notes.read(store, ALBUM)
    assert note['description']['text'] == 'Album prose.'
    assert note['editorial_version'] == 5
    store.rec_proposals.request_album_notes([ALBUM], 3)
    assert not store.rec_proposals.get_proposals('album_note_requests')


def test_cached_wikipedia_and_musicbrainz_do_not_leak_during_refresh_or_outage(store, monkeypatch):
    store.rec_proposals.publish_album_note(ALBUM['browse_id'], {
        'artist_note': 'An artist biography.', 'artist_url': 'https://en.wikipedia.org/wiki/Artist',
        'description': {'text': 'Album Wikipedia prose.', 'url': 'https://en.wikipedia.org/wiki/Album'},
        'rating': {'value': 90, 'scale': 100, 'url': 'https://musicbrainz.org/release-group/id'},
        'links': [{'label': 'MusicBrainz', 'url': 'https://musicbrainz.org/release-group/id'}],
        'editorial_version': 2, 'retry_at': 999999}, 1)
    note = album_notes.read(store, ALBUM)
    assert 'artist_note' not in note and not note['description'] and not note['rating'] and not note['links']
    monkeypatch.setattr(wikipedia, 'fetch_summary', lambda *a: pytest.fail('No Wikipedia for Albums'))
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {'evidence': [], 'retry': True})
    album_notes.prepare(store, 2, [ALBUM])
    assert not album_notes.read(store, ALBUM)['description']


def test_worker_artwork_uses_matching_artist_results_not_album_thumbnails():
    from types import SimpleNamespace
    rows = [
        {'artist': ALBUM['artist'], 'resultType': 'album', 'browseId': 'UCwrong',
         'thumbnails': [{'url': 'https://example.com/album.jpg', 'width': 100}]},
        {'artist': 'Someone Else', 'resultType': 'artist', 'browseId': 'UCother',
         'thumbnails': [{'url': 'https://example.com/wrong.jpg', 'width': 100}]},
        {'artist': ALBUM['artist'], 'resultType': 'artist', 'browseId': 'UCduo',
         'thumbnails': [{'url': 'https://example.com/portrait.jpg', 'width': 100}]},
    ]
    client = SimpleNamespace(search=lambda name, filter: rows)
    art = album_notes._artist_artwork(client, ALBUM)
    assert art['url'] == 'https://example.com/portrait.jpg'
    assert art['kind'] == 'artist' and art['source_url'].endswith('/UCduo')


def test_worker_fills_missing_copy_with_verified_album_facts_without_saving(store, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {'evidence': [], 'retry': False})
    album = {**ALBUM, 'browse_id': 'MPREb_x'}
    release = {'title': album['title'], 'artists': [{'name': album['artist']}], 'year': '2024',
               'trackCount': 2, 'tracks': [{'title': 'Opener', 'videoId': 'v1', 'duration': '1:00'},
                                         {'title': 'Closer', 'videoId': 'v2', 'duration': '2:00'}]}
    calls = []
    client = SimpleNamespace(get_album=lambda browse: calls.append(browse) or release,
                             search=lambda *args, **kwargs: [])
    album_notes.prepare(store, 100, [album], client=client)
    note = album_notes.read(store, album)
    assert note['description']['text'] == '2 tracks, released in 2024, 3 minutes in all. Opens with Opener and closes with Closer.'
    assert note['description']['source'] == 'YouTube Music · release details'
    assert note['description']['kind'] == 'catalog' and not note['description']['quote']
    assert store.collection.album_listening_summary(['MPREb_x'])['MPREb_x'] == {
        'plays': 0, 'tracks': 2, 'complete': True}
    # Library completeness deliberately excludes fetched, unsaved catalog entries.
    assert not store.collection.is_album_tracklist_complete('MPREb_x')
    assert not store.collection.saved_album_ids()
    assert store.conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0] == 0
    assert calls == ['MPREb_x']
    evidence = store.rec_ledger.evidence().get('artifact', note['evidence'])
    assert any(row['provider'] == 'youtube_album' for row in evidence['sources'])


@pytest.mark.parametrize('changed', [
    {'title': 'Different record'}, {'artists': [{'name': 'Someone Else'}]},
    {'trackCount': 3}, {'continuations': ['unfinished']},
])
def test_catalog_copy_rejects_wrong_editions_artists_and_partial_tracklists(store, changed):
    from types import SimpleNamespace
    album = {**ALBUM, 'browse_id': 'MPREb_x'}
    release = {'title': album['title'], 'artists': [{'name': album['artist']}],
               'trackCount': 1, 'tracks': [{'title': 'One', 'videoId': 'v1'}], **changed}
    assert album_notes._catalog_description(SimpleNamespace(get_album=lambda browse: release), album, store) == (None, None)
    assert not store.collection.cached_album_tracklist_ids()


def test_upgraded_copy_preserves_existing_art_and_previous_prose_on_outage(store, monkeypatch):
    art = {'url': 'https://example.com/artist.jpg', 'source_url': 'https://example.com/artist',
           'kind': 'artist', 'credit': 'Artist photograph'}
    description = {'text': 'A checked, album-specific account.', 'url': 'https://example.com/review',
                   'source': 'Album review', 'acquisition': 'automatic'}
    previous = store.rec_ledger.evidence().artifact({'sources': ['original release evidence']})
    store.rec_proposals.publish_album_note(ALBUM['browse_id'], {
        'description': description, 'artwork': art, 'evidence': previous,
        'editorial_version': 3, 'retry_at': 999999}, 1)
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {'description': None, 'evidence': [], 'retry': True})
    album_notes.prepare(store, 2, [ALBUM])
    note = album_notes.read(store, ALBUM)
    assert note['description'] == description and note['artwork'] == art
    assert note['editorial_version'] == 5 and note['retry_at'] == 3602


def test_hand_written_cache_is_excluded_and_worker_gets_automatic_copy(store, monkeypatch):
    store.rec_proposals.publish_album_note(ALBUM['browse_id'], {
        'description': {'text': 'A hand-written album paragraph.', 'source': 'Research ledger',
                        'url': 'https://example.com/release'},
        'editorial_version': 4, 'retry_at': 999999}, 1)
    assert not album_notes.read(store, ALBUM)['description']
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {'evidence': [], 'retry': False})
    description = {'text': 'Verified album facts.', 'source': 'Deezer · release details',
                   'url': 'https://www.deezer.com/album/42', 'kind': 'catalog',
                   'acquisition': 'automatic'}
    monkeypatch.setattr(album_facts, 'from_deezer', lambda *args:
                        (description, {'provider': 'deezer_album', 'response': {'id': 42}}))
    album_notes.prepare(store, 2, [ALBUM])
    assert album_notes.read(store, ALBUM)['description'] == description
