"""Contract tests for the htmx Save/Unsave album actions (/collection/save-album|unsave-album).

The routes now do their YouTube/store work and return an empty 200 with HX-Refresh: true
(htmx reloads, keeping both album tables + the saved column in sync, parity with the old
location.reload()), and surface failures via the standard 422 OOB error toast, instead of
the old JSON payloads.
"""
from fastapi.testclient import TestClient
import pytest

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
from tests.home_fixture_support import home_control_routes, home_routes_only, without_metadata_providers
from tests.store_fixture_support import _file_consumer_store, bounded_consumer_schema


class AlbumClient(FakeClient):
    """FakeClient that can resolve + rate an album. Pass album={} to simulate a bad album."""
    def __init__(self, album=None, **kw):
        super().__init__(**kw)
        self.rated = []
        self._album = {"audioPlaylistId": "PLAUDIO", "title": "The Album", "type": "Album",
                       "artists": [{"name": "Artist X"}], "year": "2001", "thumbnails": []} \
            if album is None else album

    def get_album(self, browse_id):
        return self._album

    def rate_playlist(self, playlist_id, rating):
        self.rated.append((playlist_id, rating))


def _client(store, provider):
    return TestClient(create_app(store, provider, now_fn=lambda: 1.0), base_url="http://127.0.0.1")


def _refreshes(r):
    return r.status_code == 200 and r.headers.get("hx-refresh") == "true"


def test_artists_page_includes_saved_albums_without_playlists(store):
    store.collection.add_saved_album({"browse": "B1", "title": "The Album", "artist": "Album artist"})
    store.tracks.upsert_track("v1", "Album song", "Album artist", "The Album", 200, album_browse_id="B1")
    client = _client(store, lambda: {})

    response = client.get("/artists")
    assert response.status_code == 200
    assert "across your playlists and saved albums" in response.text
    artist, = response.context["artists"]
    assert (artist["artist"], artist["songs"], artist["n_albums"], artist["n_pls"]) == (
        "Album artist", 1, 1, 0)
    assert '/artist?name=Album%20artist' in response.text


@pytest.mark.parametrize('headers,content_type', [({}, 'text/html'),
    ({'HX-Request': 'true'}, None), ({'Accept': 'application/json'}, 'application/json')])
def test_artists_read_deadline_defers_without_background_error(store, monkeypatch, caplog, headers, content_type):
    from unittest.mock import Mock

    from yt_playlist.core.activity import check_stopping, work_budget

    def exhausted():
        with work_budget(0):
            check_stopping()

    app = create_app(store, lambda: {}, background_work=False)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        with monkeypatch.context() as patch:
            patch.setattr(store.collection, 'collection_artists', exhausted)
            def forbidden():
                pytest.fail('Recovery template tried another database-backed helper')
            for helper in ('storage_maintenance_status', 'initial_setup_pending',
                           'has_multiple_identities'):
                patch.setitem(app.state.ctx.templates.env.globals, helper, forbidden)
            # These read environment settings or model files, not the database.
            # Keep their real behavior available in the recovery document.
            pure_helpers = {name: Mock(wraps=app.state.ctx.templates.env.globals[name])
                            for name in ('audio_preview_available', 'audio_vibes_enabled')}
            for name, helper in pure_helpers.items():
                patch.setitem(app.state.ctx.templates.env.globals, name, helper)
            response = client.get('/artists', headers=headers)
            for helper in pure_helpers.values():
                assert helper.called == (content_type == 'text/html')
        assert response.status_code == 503
        assert response.headers['Retry-After'] == '2'
        assert response.headers['X-Read-Pending'] == 'true'
        assert response.headers['Cache-Control'] == 'no-store'
        if content_type == 'text/html':
            assert 'Loading your library' in response.text
            assert 'id="app-page"' in response.text
            assert 'read-recovery.js' in response.text
        elif content_type == 'application/json':
            assert response.json() == {'pending': True}
        else:
            assert response.content == b''
        assert 'time limit' not in response.text
        assert 'Background preparation' not in response.text
        warnings = [record for record in caplog.records
                    if record.name == 'yt_playlist.web.app' and 'Request budget exceeded' in record.message]
        assert len(warnings) == 1
        assert 'GET /artists' in warnings[0].message
        assert warnings[0].exc_info is None
        assert client.get('/artists').status_code == 200
        assert client.get('/storage-maintenance/status').status_code == 200


@pytest.fixture
def deadline_library(tmp_path):
    # Keep instrumented handle admission, the actual SQL interrupt and a fresh
    # private reader. This fixture does not replace the timeout/cancellation
    # behavior checked below.
    with bounded_consumer_schema(()) as schema:
        with _file_consumer_store(tmp_path, schema) as store:
            yield store


@pytest.fixture
def deadline_routes(home_control_routes, without_metadata_providers):
    """Retain the actual card/control routers below request recovery."""


def test_home_card_deadline_preserves_the_board_and_recovers(deadline_library, deadline_routes,
                                                            monkeypatch, caplog):
    import sqlite3
    from tests.home_fixture_support import publish_menu, returning_listener
    from yt_playlist.core.activity import work_budget
    from yt_playlist.web.routes.home_views import HomeViews

    store = deadline_library
    store.accounts.upsert_account('main', 'cred', None, True)
    returning_listener(store, 'mixes', now=1000)
    publish_menu(store, now=1000)
    original = HomeViews.card_context
    connections = []
    expire = False

    def context(views, request, now, **options):
        if expire:
            with views.store.read_view() as reader:
                connections.append(reader.conn)
                with work_budget(.005):
                    reader.conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                        'SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n').fetchone()
        return original(views, request, now, **options)

    monkeypatch.setattr(HomeViews, 'card_context', context)
    with TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000, background_work=False)) as client:
        baseline = client.get('/home/cards', headers={'HX-Request': 'true'})
        assert baseline.status_code == 200 and 'id="gen-wheelhouse"' in baseline.text
        expire = True
        deferred = client.get('/home/cards', headers={'HX-Request': 'true'})
        assert deferred.status_code == 503 and deferred.content == b''
        assert deferred.headers['X-Read-Pending'] == 'true'
        assert not any(record.exc_info for record in caplog.records)
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connections[0].execute('SELECT 1')
        expire = False
        recovered = client.get('/home/cards', headers={'HX-Request': 'true'})
        assert recovered.status_code == 200 and 'id="gen-wheelhouse"' in recovered.text


def test_save_album_likes_and_refreshes(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    fc = AlbumClient()
    c = _client(store, lambda: {iid: fc})

    r = c.post("/collection/save-album", data={"browse_id": "MPREb_x"})
    assert _refreshes(r) and r.text == ""
    assert any(a["browse"] == "MPREb_x" for a in store.collection.get_saved_albums())   # mirrored locally
    assert fc.rated == [("PLAUDIO", "LIKE")]                                  # liked on YouTube


def test_unsave_album_unlikes_and_refreshes(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    store.collection.add_saved_album({"browse": "MPREb_x", "title": "The Album", "artist": "Artist X",
                           "year": "2001", "thumbnail": ""},account_id=iid)
    fc = AlbumClient()
    c = _client(store, lambda: {iid: fc})

    r = c.post("/collection/unsave-album", data={"browse_id": "MPREb_x"})
    assert _refreshes(r)
    assert all(a["browse"] != "MPREb_x" for a in store.collection.get_saved_albums())
    assert fc.rated == [("PLAUDIO", "INDIFFERENT")]


def test_unsave_targets_unique_non_main_owner_and_carries_owner_in_both_views(store):
    main = store.accounts.upsert_account("Main", "cred", None, True)
    other = store.accounts.upsert_account("Other", "cred", "other", False)
    store.collection.add_saved_album({"browse": "B", "title": "Album", "artist": "Artist X"}, account_id=other)
    clients = {main: AlbumClient(), other: AlbumClient()}
    c = _client(store, lambda: clients)
    for path in ("/albums", "/artist?name=Artist%20X"):
        html = c.get(path).text
        assert f'name="identity_id" value="{other}"' in html
        assert "Other" in html
    response = c.post("/collection/unsave-album", data={"browse_id": "B"})
    assert _refreshes(response)
    assert clients[main].rated == []
    assert clients[other].rated == [("PLAUDIO", "INDIFFERENT")]
    assert store.collection.get_saved_albums(account_id=other) == []


@pytest.mark.parametrize("selected", ["main", "other"])
def test_multiple_owners_require_choice_and_remove_only_selected_membership(store, selected):
    main = store.accounts.upsert_account("Main", "cred", None, True)
    other = store.accounts.upsert_account("Other", "cred", "other", False)
    for owner in (main, other):
        store.collection.add_saved_album({"browse": "B", "title": "Album", "artist": "Artist X"}, account_id=owner)
    clients = {main: AlbumClient(), other: AlbumClient()}
    c = _client(store, lambda: clients)
    html = c.get("/albums").text
    assert "Unsave from Main" in html and "Unsave from Other" in html
    assert c.post("/collection/unsave-album", data={"browse_id": "B"}).status_code == 422
    assert all(client.rated == [] for client in clients.values())
    chosen, retained = (main, other) if selected == "main" else (other, main)
    response = c.post("/collection/unsave-album", data={"browse_id": "B", "identity_id": chosen})
    assert _refreshes(response)
    assert clients[chosen].rated == [("PLAUDIO", "INDIFFERENT")]
    assert clients[retained].rated == []
    assert store.collection.get_saved_albums(account_id=chosen) == []
    assert [a['browse'] for a in store.collection.get_saved_albums(account_id=retained)] == ['B']


@pytest.mark.parametrize("owner_state", ["unavailable", "inactive"])
def test_unavailable_album_owner_never_falls_back_to_main(store, owner_state):
    main = store.accounts.upsert_account("Main", "cred", None, True)
    other = store.accounts.upsert_account("Other", "cred", "other", False)
    store.collection.add_saved_album({"browse": "B", "title": "Album"}, account_id=other)
    clients = {main: AlbumClient()}
    if owner_state == "inactive":
        with store.conn:
            store.conn.execute('UPDATE accounts SET active=0 WHERE id=?', (other,))
        clients[other] = AlbumClient()
    c = _client(store, lambda: clients)
    response = c.post("/collection/unsave-album", data={"browse_id": "B", "identity_id": other})
    assert response.status_code == 422 and "not connected" in response.text
    assert all(client.rated == [] for client in clients.values())
    assert [a['browse'] for a in store.collection.get_saved_albums(account_id=other)] == ['B']
    assert store.collection.saved_album_owners('B')['B'][0]['account_id'] == other


def test_album_unsave_rejects_non_owner_and_unknown_legacy_ownership(store):
    main = store.accounts.upsert_account("Main", "cred", None, True)
    other = store.accounts.upsert_account("Other", "cred", "other", False)
    store.collection.add_saved_album({"browse": "owned", "title": "Owned"}, account_id=other)
    store.collection.add_saved_album({"browse": "legacy", "title": "Legacy"})
    clients = {main: AlbumClient(), other: AlbumClient()}
    c = _client(store, lambda: clients)
    for data in ({"browse_id": "owned", "identity_id": main}, {"browse_id": "legacy"},
                 {"browse_id": "legacy", "identity_id": main}, {"browse_id": "owned", "identity_id": "bad"}):
        assert c.post("/collection/unsave-album", data=data).status_code == 422
    assert all(client.rated == [] for client in clients.values())
    assert store.collection.saved_album_ids() == {'owned', 'legacy'}


def test_save_album_without_id_returns_toast(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    c = _client(store, lambda: {iid: AlbumClient()})
    r = c.post("/collection/save-album", data={})
    assert r.status_code == 422
    assert r.headers.get("hx-reswap") == "none"
    assert 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert "no album id" in r.text
    assert store.collection.get_saved_albums() == []


def test_save_album_client_error_returns_toast(store):
    iid = store.accounts.upsert_account("main", "cred", None, True)
    fc = AlbumClient(album={})                       # no audioPlaylistId -> ValueError path
    c = _client(store, lambda: {iid: fc})
    r = c.post("/collection/save-album", data={"browse_id": "X"})
    assert r.status_code == 422
    assert 'hx-swap-oob="afterbegin:#toasts"' in r.text
    assert store.collection.get_saved_albums() == []           # nothing saved on failure
    assert fc.rated == []


@pytest.mark.parametrize('ack', [False, {'status': 'STATUS_FAILED'}, {'status': 'STATUS_ERROR'}])
@pytest.mark.parametrize('saved', [False, True])
def test_rejected_album_change_preserves_account_memberships(store, ack, saved):
    main = store.accounts.upsert_account('Main', 'cred', None, True)
    other = store.accounts.upsert_account('Other', 'cred', 'other', False)
    for owner in ([other] if saved else [main, other]):
        store.collection.add_saved_album({'browse': 'B', 'title': 'Album'}, account_id=owner)

    class Rejected(AlbumClient):
        def rate_playlist(self, playlist_id, rating):
            super().rate_playlist(playlist_id, rating)
            return ack

    fc = Rejected()
    client = _client(store, lambda: {main: fc, other: AlbumClient()})
    before = store.collection.saved_album_owners('B')
    response = client.post('/collection/' + ('save-album' if saved else 'unsave-album'),
                           data={'browse_id': 'B', 'identity_id': main})
    assert response.status_code == 422
    assert 'did not confirm' in response.text
    assert 'hx-refresh' not in response.headers
    assert store.collection.saved_album_owners('B') == before
    assert fc.rated == [('PLAUDIO', 'LIKE' if saved else 'INDIFFERENT')]


@pytest.mark.parametrize('ack', [None, True, {}, {'status': 'STATUS_SUCCEEDED'}])
def test_manual_and_import_album_saves_share_confirmation_and_metadata(store, ack):
    from yt_playlist.library.spotify_library import _save_album

    iid = store.accounts.upsert_account('Main', 'cred', None, True)

    class Confirmed(AlbumClient):
        def rate_playlist(self, playlist_id, rating):
            super().rate_playlist(playlist_id, rating)
            return ack

    fc = Confirmed()
    client = _client(store, lambda: {iid: fc})
    assert _refreshes(client.post('/collection/save-album', data={'browse_id': 'B'}))
    manual = store.collection.get_saved_albums(account_id=iid)
    assert _refreshes(client.post('/collection/unsave-album', data={'browse_id': 'B'}))
    assert store.collection.get_saved_albums(account_id=iid) == []
    _save_album(store, fc, {'browse_id': 'B', 'title': 'Fallback'}, iid)
    assert store.collection.get_saved_albums(account_id=iid) == manual


@pytest.mark.parametrize('ack', [False, {'status': 'STATUS_FAILED'}])
def test_import_album_rejection_does_not_publish_membership(store, ack):
    from yt_playlist.library.spotify_library import _save_album

    iid = store.accounts.upsert_account('Main', 'cred', None, True)
    fc = AlbumClient()
    fc.rate_playlist = lambda *args: ack
    with pytest.raises(ValueError, match='did not confirm'):
        _save_album(store, fc, {'browse_id': 'B', 'title': 'Fallback'}, iid)
    assert store.collection.get_saved_albums(account_id=iid) == []
