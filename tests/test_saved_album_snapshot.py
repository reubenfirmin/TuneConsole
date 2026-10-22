"""Complete saved-album snapshots replace their owner; unavailable accounts retain evidence."""
import json
from types import SimpleNamespace

import pytest

from yt_playlist.core.identities import build_client
from yt_playlist.library import sync


def _album(bid):
    return {"browseId": bid, "title": bid, "artists": [{"name": "Artist"}]}


@pytest.fixture
def mirror(store, monkeypatch):
    first = store.identities.upsert_identity('main', 'bridge', None, True)
    second = store.identities.upsert_identity('brand', 'bridge', 'brand-id', False)
    store.collection.add_saved_album({'browse': 'OLD', 'title': 'Previously saved', 'artist': 'Artist'})
    monkeypatch.setattr(sync, 'with_retry', lambda fn: fn())
    materialized = []
    monkeypatch.setattr(sync, '_materialize_album_tracks',
                        lambda store, clients, saved, progress: materialized.append(saved))
    return SimpleNamespace(store=store, first=first, second=second,
                           before=store.collection.get_saved_albums(), materialized=materialized)


class AlbumClient:
    def __init__(self, albums):
        self.albums = albums
        self.limits = []

    def get_library_albums(self, limit=25):
        self.limits.append(limit)
        if isinstance(self.albums, Exception):
            raise self.albums
        return self.albums


@pytest.mark.parametrize('responses', [
    ([_album('NEW')], TimeoutError('unavailable')),
    (TimeoutError('unavailable'), ConnectionError('disconnected')),
    ([], None),
    ([], {'albums': [], 'continuation': 'next'}),
    ([], [{'title': 'missing album id'}]),
], ids=['one-account-failed', 'all-accounts-failed', 'unavailable', 'incomplete', 'unidentified'])
def test_failed_or_incomplete_account_retains_its_previous_evidence(mirror, responses):
    events = []
    sync._sync_saved_albums(mirror.store, dict(zip(
        (mirror.first, mirror.second), map(AlbumClient, responses))), events.append)
    expected = {'OLD','NEW'} if isinstance(responses[0],list) and responses[0] else {'OLD'}
    assert mirror.store.collection.saved_album_ids()==expected
    if not isinstance(responses[0],list):
        assert mirror.materialized==[]
        assert 'unchanged' in events[-1]['text']


@pytest.mark.parametrize('skipped', ['one', 'all'])
def test_skipped_accounts_preserve_the_shared_mirror(mirror, skipped):
    clients = {mirror.first: AlbumClient([])} if skipped == 'one' else {}
    sync._sync_saved_albums(mirror.store, clients, None)
    assert mirror.store.collection.get_saved_albums() == mirror.before


def test_verified_empty_libraries_clear_the_previous_mirror(mirror):
    sync._sync_saved_albums(mirror.store,
                            {mirror.first: AlbumClient([]), mirror.second: AlbumClient([])}, None)
    assert mirror.store.collection.get_saved_albums() == []


def test_successful_account_refresh_preserves_failed_accounts_snapshot(mirror):
    mirror.store.collection.replace_saved_album_snapshots({
        mirror.first:[{'browse':'FIRST','title':'First'}],
        mirror.second:[{'browse':'SECOND','title':'Second'}]},now=1)
    outcomes,summary,_ = sync._sync_saved_albums(mirror.store,{
        mirror.first:AlbumClient([_album('NEW')]),
        mirror.second:AlbumClient(TimeoutError('offline'))},None)
    assert mirror.store.collection.saved_album_ids()=={'NEW','SECOND'}
    assert {a['browse'] for a in mirror.store.collection.get_saved_albums(identity_id=mirror.second)}=={'SECOND'}
    assert summary['status']=='partial'


def test_all_accounts_are_fetched_without_the_old_500_album_limit(mirror):
    first = AlbumClient([_album(f'ALBUM{i}') for i in range(501)])
    second = AlbumClient([_album('BRAND')])
    sync._sync_saved_albums(mirror.store, {mirror.first: first, mirror.second: second}, None)
    assert first.limits == second.limits == [None]
    assert len(mirror.store.collection.saved_album_ids()) == 502
    assert 'OLD' not in mirror.store.collection.saved_album_ids()


def _renderer(bid):
    return {'musicTwoRowItemRenderer': {
        'title': {'runs': [{'text': bid, 'navigationEndpoint': {'browseEndpoint': {'browseId': bid}}}]},
        'subtitle': {'runs': [{'text': 'Album'}, {'text': ' • '}, {'text': 'Artist'}]},
        'thumbnailRenderer': {'musicThumbnailRenderer': {'thumbnail': {'thumbnails': []}}},
    }}


def _page(*ids, continuation=None):
    grid = {'items': [_renderer(bid) for bid in ids]}
    if continuation is not None:
        grid['continuations'] = [{'nextContinuationData': {'continuation': continuation}}]
    return grid


def _first_page(grid):
    return {'contents': {'singleColumnBrowseResultsRenderer': {'tabs': [{'tabRenderer': {
        'content': {'sectionListRenderer': {'contents': [{'gridRenderer': grid}]}}}}]}}}


class SnapshotBridge:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def execute(self, method, url, body):
        self.calls.append((url, body))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return 200, json.dumps(response)


def _production_client(responses):
    bridge = SnapshotBridge(responses)
    return build_client(SimpleNamespace(brand_account_id=None), bridge), bridge


def test_production_client_reads_and_parses_every_album_page():
    client, bridge = _production_client([
        _first_page(_page('ONE', continuation='next')),
        {'continuationContents': {'gridContinuation': _page('TWO')}},
    ])
    assert [a['browseId'] for a in client.get_library_albums(limit=None)] == ['ONE', 'TWO']
    assert len(bridge.calls) == 2
    assert 'continuation=next' in bridge.calls[-1][0]


def test_production_client_accepts_explicit_empty_grid():
    client, _ = _production_client([_first_page(_page())])
    assert client.get_library_albums(limit=None) == []


@pytest.mark.parametrize('last_page', [
    {},
    {'continuationContents': {}},
    {'continuationContents': {'gridContinuation': {'items': None}}},
    {'continuationContents': {'gridContinuation': _page('TWO', continuation='next')}},
    {'continuationContents': {'gridContinuation': _page()},
     'responseContext': {'mainAppWebResponseContext': {'loggedOut': True}}},
    {'error': {'message': 'unavailable'}},
    TimeoutError('continuation timed out'),
], ids=['missing-continuation', 'missing-grid', 'invalid-items', 'repeated-token',
        'signed-out', 'error-response', 'request-failed'])
def test_partial_production_pagination_never_replaces_saved_membership(mirror, last_page):
    client, bridge = _production_client([
        _first_page(_page('NEW', continuation='next')), last_page,
    ])
    events = []
    sync._sync_saved_albums(mirror.store,
                            {mirror.first: client, mirror.second: AlbumClient([])}, events.append)
    assert len(bridge.calls) == 2
    assert mirror.store.collection.get_saved_albums() == mirror.before
    assert mirror.store.conn.execute('SELECT identity_id FROM album_library_snapshots').fetchone()[0]==mirror.second


@pytest.mark.parametrize('response', [{}, {'contents': {}},
    {'responseContext': {'mainAppWebResponseContext': {'loggedOut': True}},
     **_first_page(_page())}])
def test_missing_or_signed_out_grid_is_unavailable_not_empty(mirror, response):
    client, _ = _production_client([response])
    sync._sync_saved_albums(mirror.store,
                            {mirror.first: client, mirror.second: AlbumClient([])}, None)
    assert mirror.store.collection.get_saved_albums() == mirror.before
