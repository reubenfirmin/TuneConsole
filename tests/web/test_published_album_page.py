"""Exercise occurrence order through the real album HTTP page and expansion."""
import re
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.home_fixture_support import home_page_bytecode, without_metadata_providers
from tests.store_fixture_support import bounded_consumer_schema, file_consumer_store as store
from yt_playlist.core import albums, catalog
from yt_playlist.library.album_catalog import remember_album
from yt_playlist.library.album_fetch import UPLOAD_RELEASE_PREFIX
from yt_playlist.web.app import create_app


pytestmark = pytest.mark.usefixtures('album_routes', 'without_metadata_providers')


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


def test_album_page_input_keeps_empty_occurrences_and_real_guards(_pristine_current_schema):
    assert _pristine_current_schema.execute('PRAGMA query_only').fetchone()[0] == 1
    assert _pristine_current_schema._revision_script
    assert not _pristine_current_schema.execute('SELECT 1 FROM album_track_occurrences').fetchone()
    assert not _pristine_current_schema.execute('PRAGMA foreign_key_check').fetchone()


@pytest.fixture
def album_routes(monkeypatch, home_page_bytecode):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import album, app_update, audio_preview

    # Keep the real album controller, HTTP middleware and templates. Unrelated
    # routes and metadata-provider setup are below the page's boundary.
    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [module.build(ctx)
        for module in (app_update, audio_preview, album)])
    templates = webapp.Jinja2Templates

    def compiled(*args, **kwargs):
        result = templates(*args, **kwargs)
        result.env.bytecode_cache = home_page_bytecode
        return result

    monkeypatch.setattr(webapp, 'Jinja2Templates', compiled)
    client_type = TestClient
    with ExitStack() as clients:
        monkeypatch.setattr(__name__ + '.TestClient',
            lambda *args, **kwargs: clients.enter_context(client_type(*args, **kwargs)))
        yield


def payload(videos):
    return {'title':'Release','artists':[{'name':'Artist'}],'trackCount':len(videos),
            'tracks':[{'videoId':v,'title':v.title(),'duration':'3:00'} for v in videos]}


@pytest.mark.parametrize('ownership',['saved','unsaved','upload'])
@pytest.mark.parametrize('case',['baseline','incidental','removed','unavailable','partial','offline'])
def test_album_page_and_expansion_follow_published_occurrences(store,ownership,case):
    account = store.accounts.upsert_account('Main','credential',None,True)
    browse = UPLOAD_RELEASE_PREFIX+'release' if ownership=='upload' else 'release'
    if ownership=='saved':
        store.collection.replace_saved_albums([{'browse':browse,'title':'Release','artist':'Artist'}])
    if case=='removed':
        remember_album(store,browse,payload(['one','removed','two','one']))
    current = payload(['one','two','one'])
    if case=='unavailable':
        current['tracks'].insert(1,{'videoId':'unavailable','discNumber':1,'trackNumber':2})
        current['trackCount'] = 4
    remember_album(store,browse,current)
    if case=='incidental':
        store.tracks.upsert_track('bonus','Bonus','Artist','Release',180,album_browse_id=browse)
    live = current
    if case=='partial':
        live = payload(['prefix'])
        live['trackCount'] = 3
    elif case=='offline':
        live = {}
    class Provider(FakeClient):
        def get_library_upload_album(self, _browse):
            return live
    provider = Provider(albums={browse:live})
    client = TestClient(create_app(store,lambda:{account:provider},now_fn=lambda:1,
                                   background_work=False),base_url='http://127.0.0.1')
    expected = [t.get('videoId') if t.get('title') else '' for t in current['tracks']]
    for route in ('/album?browse=','/album/inline?browse='):
        response = client.get(route+browse)
        assert response.status_code==200
        assert len(response.context['album']['tracks'])==len(expected)
        assert re.findall(r'<tr class="srow" data-vid="([^"]*)"',response.text)==expected
        if case=='unavailable':
            assert 'Unavailable track' in response.text
            assert 'watch?v=unavailable' not in response.text
    aid = albums.lookup(store.conn,browse)
    assert [r[0] for r in store.conn.execute('SELECT o.raw_source_ref FROM albums a '
        'JOIN album_track_occurrences o ON o.tracklist_id=a.current_tracklist_id '
        'WHERE a.id=? ORDER BY o.position',(aid,))]==[t['videoId'] for t in current['tracks']]
    # Incidental evidence survives without entering the published sequence.
    if case=='incidental':
        assert store.conn.execute('SELECT 1 FROM album_item_observations i JOIN catalog_entries e ON e.id=i.entry_id '
                                  "WHERE i.album_id=? AND e.video_id='bonus'",(aid,)).fetchone()
    if ownership=='unsaved':
        assert not store.collection.saved_album_ids()
        if case!='incidental':
            assert not store.conn.execute('SELECT 1 FROM library_memberships').fetchone()


def test_published_table_keeps_local_and_unresolved_positions_and_exact_upload_choice(store):
    account = store.accounts.upsert_account('Main','credential',None,True)
    with store.conn:
        alternate = catalog.upsert(store.conn,'alternate','Alternate','Artist',library=False)
        exact = catalog.upsert(store.conn,'exact','Exact','Artist',library=False)
        local = catalog.upsert(store.conn,None,'Local title','Artist',source_ref='local:file',library=False)
        unresolved = catalog.upsert(store.conn,None,'Captured title','Artist',source_ref='unresolved:input',library=False)
    recording = store.tracks.catalog_subject(exact)['recording_id']
    store.tracks.associate_youtube_item('alternate',recording,source='verified',evidence={'same':True},now=10)
    store.collection.cache_album_tracklist('mixed','Mixed','Artist',None,[],entries=[
        {'entry_id':local},{'raw_source_ref':'unknown','captured_title':'Unresolved occurrence'},
        {'entry_id':exact},{'entry_id':unresolved},{'entry_id':exact}])
    with store.conn:
        for occurrence, in store.conn.execute('SELECT id FROM album_track_occurrences WHERE raw_source_ref=?',('exact',)).fetchall():
            store.conn.execute("INSERT INTO album_occurrence_sources VALUES (?,?,'youtube')",(occurrence,alternate))
    detail = store.charts.album_tracks_detail('mixed')
    assert [t['title'] for t in detail]==['Local title','Unresolved occurrence','Exact','Captured title','Exact']
    assert [t['video_id'] for t in detail]==['','','exact','','exact']
    assert [t['track_id'] for t in detail]==[local,None,exact,unresolved,exact]
    client = TestClient(create_app(store,lambda:{account:FakeClient()},now_fn=lambda:1,
                                   background_work=False),base_url='http://127.0.0.1')
    response = client.get('/album?browse=mixed')
    assert re.findall(r'<tr class="srow" data-vid="([^"]*)"',response.text)==['','','exact','','exact']
    assert 'Unresolved occurrence' in response.text and 'Local title' in response.text
    assert 'watch?v=alternate' not in response.text
