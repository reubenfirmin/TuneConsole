import pytest

from yt_playlist.providers import discogs, musicbrainz, release_notes, wikipedia, album_editorial, deepseek
from yt_playlist.providers.album_editorial import lookup as editorial_lookup
from yt_playlist.rec import album_notes

ALBUM = {'browse_id': 'record', 'artist': 'Hermanos Gutiérrez', 'title': 'Sonido Cósmico'}
MBID = '12345678-1234-1234-1234-123456789abc'


@pytest.fixture(autouse=True)
def healthy_providers(monkeypatch):
    monkeypatch.setattr(musicbrainz, 'tripped', lambda: False)
    monkeypatch.setattr(discogs, 'tripped', lambda: False)
    monkeypatch.setattr(album_editorial, 'lookup', lambda *args: None)
    monkeypatch.setattr(deepseek._iao, 'api_key', lambda *args: None)
    monkeypatch.setattr(wikipedia, 'fetch_artwork', lambda *args: None)


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
                              {'provider': 'deepseek', 'response': {'status': 'not_configured'}}]
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {
        'rating': None, 'links': [], 'label': None, 'track_count': None, 'evidence': [], 'retry': True})
    album_notes.prepare(store, 100 + 8 * 86400, [ALBUM])
    after = album_notes.read(store, ALBUM)
    assert after['rating'] == rating and after['label'] == 'Label'
    assert after['evidence'] == note['evidence']
    assert len(store.rec_ledger.evidence().records('artifact')) == 1
    assert not store.rec_proposals.get_proposals('album_note_requests')


def test_notes_worker_honors_long_provider_retry_after(store, monkeypatch):
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {
        'evidence': [], 'retry': True, 'retry_at': 10000})
    album_notes.prepare(store, 100, [ALBUM])
    assert album_notes.read(store, ALBUM)['retry_at'] == 10000


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
    assert note['editorial_version'] == 7
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


def test_worker_caches_complete_tracklist_for_familiarity_without_filler_copy_or_saving(store, monkeypatch):
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
    assert not note.get('description')
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
def test_catalog_metadata_rejects_wrong_editions_artists_and_partial_tracklists(store, changed):
    from types import SimpleNamespace
    album = {**ALBUM, 'browse_id': 'MPREb_x'}
    release = {'title': album['title'], 'artists': [{'name': album['artist']}],
               'trackCount': 1, 'tracks': [{'title': 'One', 'videoId': 'v1'}], **changed}
    assert album_notes._catalog_metadata(SimpleNamespace(get_album=lambda browse: release), album, store) is None
    assert not store.collection.cached_album_tracklist_ids()


@pytest.mark.parametrize('credits', [
    [{'name': 'First', 'id': 'UCfirst'}, {'name': 'Second', 'id': 'UCsecond'}],
    [{'name': 'Single, with comma', 'id': 'UCsingle'}],
])
def test_worker_enriches_uncached_saved_releases_with_ordered_credits(store, monkeypatch, credits):
    from types import SimpleNamespace
    artist = ', '.join(credit['name'] for credit in credits)
    album = {'browse_id': 'MPREjoint', 'title': 'Joint record', 'artist': artist}
    store.collection.add_saved_album({'browse': album['browse_id'], 'title': album['title'],
                                      'artist': artist, 'credits': credits})
    assert not store.collection.album_listening_summary([album['browse_id']])[album['browse_id']]['complete']
    release = {'title': album['title'], 'artists': credits, 'trackCount': 1,
               'tracks': [{'title': 'Opener', 'videoId': 'joint-track'}]}
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {'evidence': [], 'retry': False})
    client = SimpleNamespace(get_album=lambda browse: release, search=lambda *args, **kwargs: [])
    assert not album_notes.prepare(store, 100, [album], client=client)
    assert store.collection.album_listening_summary([album['browse_id']])[album['browse_id']] == {
        'plays': 0, 'tracks': 1, 'complete': True}
    assert [c['browse_id'] for c in store.artists.album_credits(album['browse_id'])] == [c['id'] for c in credits]


@pytest.mark.parametrize('changed', [
    {'artists': [{'name': 'First', 'id': 'UCdifferent'}, {'name': 'Second', 'id': 'UCsecond'}]},
    {'artists': [{'name': 'Second', 'id': 'UCsecond'}, {'name': 'First', 'id': 'UCfirst'}]},
    {'artists': [{'name': 'First', 'id': 'UCfirst'}]},
    {'title': 'Joint record (Live)'},
    {'trackCount': 2},
])
def test_joint_release_validation_preserves_identity_order_edition_and_completeness_checks(store, changed):
    from types import SimpleNamespace
    credits = [{'name': 'First', 'id': 'UCfirst'}, {'name': 'Second', 'id': 'UCsecond'}]
    album = {'browse_id': 'MPREjoint', 'title': 'Joint record', 'artist': 'First, Second'}
    store.collection.add_saved_album({'browse': album['browse_id'], 'title': album['title'],
                                      'artist': album['artist'], 'credits': credits})
    release = {'title': album['title'], 'artists': credits, 'trackCount': 1,
               'tracks': [{'title': 'Opener', 'videoId': 'joint-track'}], **changed}
    assert album_notes._catalog_metadata(SimpleNamespace(get_album=lambda _: release), album, store) is None
    assert not store.collection.cached_album_tracklist_ids()


def test_unresolved_combined_credit_and_discovered_primary_can_gain_structured_collaborators(store):
    from types import SimpleNamespace
    credits = [{'name': 'First', 'id': 'UCfirst'}, {'name': 'Second', 'id': 'UCsecond'}]
    store.collection.add_saved_album({'browse': 'legacy', 'title': 'Joint', 'artist': 'First, Second'})
    first = store.artists.ensure_artist('First', provider='youtube', provider_id='UCfirst')
    store.discovery.upsert_discovered_album('discovery', 'First', 'Joint', None, None, 1, artist_id=first)
    release = {'title': 'Joint', 'artists': credits, 'trackCount': 1,
               'tracks': [{'title': 'Opener', 'videoId': 'joint-track'}]}
    client = SimpleNamespace(get_album=lambda _: release)
    for browse, artist in [('legacy', 'First, Second'), ('discovery', 'First')]:
        assert album_notes._catalog_metadata(client, {'browse_id': browse, 'title': 'Joint', 'artist': artist}, store)
        assert [c['browse_id'] for c in store.artists.album_credits(browse)] == ['UCfirst', 'UCsecond']


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
    assert note['editorial_version'] == 7 and note['retry_at'] == 3602


def test_missing_artist_search_art_uses_public_photograph_without_biography(store, monkeypatch):
    from types import SimpleNamespace
    art = {'url': 'https://upload.wikimedia.org/portrait.jpg',
           'source_url': 'https://commons.wikimedia.org/wiki/File:Portrait.jpg',
           'kind': 'artist', 'credit': 'Robert Hood · Photographer · CC BY-SA 4.0'}
    album = {**ALBUM, 'artist': 'Robert Hood'}
    store.rec_proposals.publish_album_note(album['browse_id'], {
        'editorial_version': 6, 'retry_at': 999999}, 1)
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {'evidence': [], 'retry': False})
    monkeypatch.setattr(wikipedia, 'fetch_artwork', lambda artist: art if artist == 'Robert Hood' else None)
    def unavailable(*args, **kwargs):
        raise OSError('catalogue offline')
    client = SimpleNamespace(search=unavailable, get_album=unavailable)
    album_notes.prepare(store, 2, [album], client=client)
    note = album_notes.read(store, album)
    assert note['artwork'] == art
    assert not note.get('artist_note') and not note.get('description')
    assert note['editorial_version'] == 7
    evidence = store.rec_ledger.evidence().get('artifact', note['evidence'])
    assert {'provider': 'album_artwork', 'response': art} in evidence['sources']


def test_thumbnail_upgrade_retains_art_during_outage_and_retries(store, monkeypatch):
    old = {'url': 'https://example.com/small.jpg', 'source_url': 'https://music.youtube.com/channel/UCartist',
           'kind': 'artist', 'credit': 'Artist'}
    store.rec_proposals.publish_album_note(ALBUM['browse_id'], {
        'editorial_version': 6, 'retry_at': 999999, 'artwork': old}, 1)
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {'evidence': [], 'retry': False})
    def unavailable(*args):
        raise wikipedia.LookupUnavailable('offline')
    monkeypatch.setattr(wikipedia, 'fetch_artwork', unavailable)
    album_notes.prepare(store, 2, [ALBUM])
    assert album_notes.read(store, ALBUM)['artwork'] == old
    assert album_notes.read(store, ALBUM)['retry_at'] == 3602
    upgraded = {**old, 'url': 'https://upload.wikimedia.org/1200px-photo.jpg',
                'source_url': 'https://commons.wikimedia.org/wiki/File:Photo.jpg'}
    monkeypatch.setattr(wikipedia, 'fetch_artwork', lambda *args: upgraded)
    album_notes.prepare(store, 3602, [ALBUM])
    assert album_notes.read(store, ALBUM)['artwork'] == upgraded


def test_hand_written_cache_is_excluded_and_worker_gets_automatic_copy(store, monkeypatch):
    store.rec_proposals.publish_album_note(ALBUM['browse_id'], {
        'description': {'text': 'A hand-written album paragraph.', 'source': 'Research ledger',
                        'url': 'https://example.com/release'},
        'editorial_version': 4, 'retry_at': 999999}, 1)
    assert not album_notes.read(store, ALBUM)['description']
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {'evidence': [], 'retry': False})
    monkeypatch.setattr(deepseek._iao, 'api_key', lambda *args: 'test-key')
    monkeypatch.setattr(deepseek, 'describe', lambda album, key: {
        'description': 'A generated, album-specific description.', 'confidence': 'moderate',
        'uncertainty': '', 'model': deepseek.MODEL, 'usage': {}, 'prompt_version': 1})
    album_notes.prepare(store, 2, [ALBUM])
    description = album_notes.read(store, ALBUM)['description']
    assert description['text'] == 'A generated, album-specific description.'
    assert description['kind'] == 'generated' and not description['quote']


def test_old_catalogue_filler_is_hidden_and_replaced_by_persistent_generated_copy(store, monkeypatch):
    store.rec_proposals.publish_album_note(ALBUM['browse_id'], {
        'description': {'text': 'Catalogued as Electro, Dance.', 'source': 'Deezer',
                        'url': 'https://www.deezer.com/album/42', 'kind': 'catalog',
                        'acquisition': 'automatic'},
        'editorial_version': 5, 'retry_at': 999999}, 1)
    assert not album_notes.read(store, ALBUM)['description']
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {'evidence': [], 'retry': False})
    monkeypatch.setattr(deepseek._iao, 'api_key', lambda *args: 'test-key')
    calls = []
    def generate(album, key):
        calls.append(album)
        return {'description': 'Album-specific prose.', 'model': deepseek.MODEL, 'usage': {}}
    monkeypatch.setattr(deepseek, 'describe', generate)
    album_notes.prepare(store, 2, [{**ALBUM, 'year': '2024'}])
    assert album_notes.read(store, ALBUM)['description']['text'] == 'Album-specific prose.'
    assert calls[0]['year'] == '2024'
    # Refreshing optional public metadata does not buy the description again.
    album_notes.prepare(store, 2 + 8 * 86400, [ALBUM])
    assert len(calls) == 1


def test_refreshed_menu_discards_old_queue_without_paying_for_hidden_records(store, monkeypatch):
    old = {**ALBUM, 'browse_id': 'previous', 'title': 'Previous album'}
    store.rec_proposals.request_album_notes([old], 1)
    monkeypatch.setattr(release_notes, 'fetch', lambda *args: {'evidence': [], 'retry': False})
    monkeypatch.setattr(deepseek._iao, 'api_key', lambda *args: 'test-key')
    calls = []
    def generate(album, key):
        calls.append(album['browse_id'])
        return {'description': 'Album-specific prose.', 'model': deepseek.MODEL, 'usage': {}}
    monkeypatch.setattr(deepseek, 'describe', generate)
    assert not album_notes.prepare(store, 2, [ALBUM])
    assert album_notes.read(store, ALBUM)['description']['text'] == 'Album-specific prose.'
    assert not album_notes.prepare(store, 3, [ALBUM])
    assert calls == [ALBUM['browse_id']]
    assert not album_notes.read(store, old)['description']
    assert store.rec_proposals.get_proposals('album_description_usage')['requests'] == 1
    # The discarded record has no fresh negative cache to suppress it later.
    assert not album_notes.prepare(store, 4, [old])
    assert calls == [ALBUM['browse_id'], old['browse_id']]


@pytest.mark.parametrize('playing', [False, True])
def test_home_worker_serves_explicit_album_page_demand_outside_its_menu(store, monkeypatch, playing):
    from types import SimpleNamespace
    from tests.test_album_web import _client
    from yt_playlist.rec import album_choices, home_prepared
    from yt_playlist.rec.worker_tasks import ServingTasks

    client, _, _ = _client(store)
    assert client.get('/album?browse=MPREb_x').status_code == 200
    assert store.rec_proposals.get_proposals('album_note_requests')['MPREb_x']['requested_until'] > 1
    old = {**ALBUM, 'browse_id': 'obsolete'}
    store.rec_proposals.request_album_notes([old], 1)
    calls = []
    monkeypatch.setattr(release_notes, 'fetch', lambda album, _: calls.append(album['browse_id'])
                        or {'evidence': [], 'retry': False})

    def stage(name, target, *args, **kwargs):
        if target is album_choices.prepare_cached:
            return {'items': [ALBUM]}
        if target is home_prepared.prepare:
            return {'menus': [1], 'modes': [], 'play_exposure': []}
        if target is album_notes.prepare:
            return target(*args, **kwargs)

    ctx = SimpleNamespace(store=store, now_fn=lambda: 2, client_provider=lambda: {},
                          bridge=SimpleNamespace(now_playing={'album_browse_id': 'MPREb_x'} if playing else {}))
    assert ServingTasks(ctx, stage).home()
    assert calls == ['MPREb_x']
    assert 'MPREb_x' not in store.rec_proposals.get_proposals('album_note_requests')
    assert 'obsolete' not in store.rec_proposals.get_proposals('album_note_requests')
    assert not ServingTasks(ctx, stage).home()
    assert calls == ['MPREb_x', ALBUM['browse_id']]


def test_visible_demand_is_coalesced_bounded_and_expires(store):
    albums = [{**ALBUM, 'browse_id': f'visible-{i}'} for i in range(30)]
    store.rec_proposals.request_album_notes(albums, 1, visible=True)
    store.rec_proposals.request_album_notes([albums[-1]], 10, visible=True)
    queued = store.rec_proposals.get_proposals('album_note_requests')
    assert len(queued) == 24 and queued['visible-29']['requested_until'] == 910
    store.rec_proposals.request_album_notes([], 902, current_only=True)
    assert set(store.rec_proposals.get_proposals('album_note_requests')) == {'visible-29'}
    store.rec_proposals.request_album_notes([], 911, current_only=True)
    assert not store.rec_proposals.get_proposals('album_note_requests')


def test_held_menu_poll_preserves_demand_after_preparation_changes(store):
    from tests.test_album_web import _client
    from yt_playlist.rec import choice_evidence
    client, _, _ = _client(store)
    opportunity = choice_evidence.present(store, 'albums', [{**ALBUM, 'id': ALBUM['browse_id']}], 1,
                                          context={}, window=0)
    assert client.get('/home/album-notes', params={
        'browse': ALBUM['browse_id'], 'opportunity': opportunity}).status_code == 200
    store.rec_proposals.request_album_notes([], 2, current_only=True)
    assert ALBUM['browse_id'] in store.rec_proposals.get_proposals('album_note_requests')
