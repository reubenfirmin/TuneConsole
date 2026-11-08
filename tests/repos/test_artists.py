import pytest

from tests.conftest import FakeClient
from tests.artist_upgrade_support import artist_legacy_input, completed_artist_input
from tests.catalog_migration_support import catalog_reader
from yt_playlist.core import artist_credits
from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.library.album_catalog import remember_album
from yt_playlist.library.sync import refresh_playlist
from yt_playlist.rec import artist_model


def test_playlist_and_album_import_keep_all_ordered_credits(store):
    identity = store.accounts.upsert_account('main','credential',None,True)
    source = {'videoId':'v','title':'Song','artists':[{'name':'First','id':'UC1'},
              {'name':'Second, with comma','id':'UC2'}], 'duration_seconds':200}
    client = FakeClient(tracks={'P':[source]})
    refresh_playlist(store,identity,client,'P','Mix',1)
    entry = store.conn.execute("SELECT id FROM catalog_tracks WHERE video_id='v'").fetchone()[0]
    credits = store.artists.track_credits(entry)
    assert [(c['credit'],c['browse_id'],c['position']) for c in credits] == [
        ('First','UC1',0),('Second, with comma','UC2',1)]
    remember_album(store,'album',{'title':'Album','artists':source['artists'],'tracks':[source]})
    assert store.artists.track_credits(entry) == credits
    assert store.conn.execute('SELECT artist FROM catalog_tracks').fetchone()[0] == 'First, Second, with comma'
    assert len(store.collection.collection_artists()) == 2
    assert {a['browse'] for a in store.collection.collection_artists()} == {'UC1','UC2'}


def test_provider_ids_keep_homonyms_distinct_and_aliases_stable(store):
    a = store.tracks.upsert_track('a','A','Same',None,None,credits=[{'name':'Same','id':'artist-a'}])
    b = store.tracks.upsert_track('b','B','Same',None,None,credits=[{'name':'Same','id':'artist-b'}])
    a_id = store.artists.track_credits(a)[0]['artist_id']
    b_id = store.artists.track_credits(b)[0]['artist_id']
    assert a_id != b_id
    assert store.artists.resolve('Same') is None
    store.tracks.upsert_track('a','A','New spelling',None,None,
                             credits=[{'name':'New spelling','id':'artist-a'}])
    assert store.artists.track_credits(a)[0]['artist_id'] == a_id
    assert store.artists.resolve('New spelling') == a_id
    store.discovery.mark_scanned(a_id,10)
    store.artist_similarity.cache_similar(a_id,[['Neighbor A',.9]],10)
    store.artist_similarity.cache_similar(b_id,[['Neighbor B',.8]],20)
    assert store.artist_similarity.cached_similar(a_id,30) == [['Neighbor A',.9]]
    assert store.artist_similarity.cached_similar(b_id,30) == [['Neighbor B',.8]]
    assert store.artist_similarity.cached_similar('Same',30) is None
    store.discovery.upsert_discovered_artist('Same',1,[],[],None,10,artist_id=a_id)
    store.discovery.upsert_discovered_artist('Same',2,[],[],None,20,artist_id=b_id)
    assert len(store.discovery.get_discovered_artists()) == 2
    store.discovery.mark_offered('artist',[a_id],30)
    counts = {r['artist_id']:r['offered_count'] for r in store.discovery.get_discovered_artists()}
    assert counts == {a_id:1,b_id:0}
    store.discovery.record_discovery_impressions('artist',[f'@artist:{b_id}'],'page',35)
    counts = {r['artist_id']:r['offered_count'] for r in store.discovery.get_discovered_artists()}
    assert counts == {a_id:1,b_id:1}
    store.discovery.upsert_discovered_album('release-a','Same','A',None,None,30,artist_id=a_id)
    store.discovery.upsert_discovered_album('release-b','Same','B',None,None,30,artist_id=b_id)
    assert set(store.discovery.discovered_albums_for_artist(a_id)) == {'release-a'}
    assert set(store.discovery.discovered_albums_for_artist(b_id)) == {'release-b'}
    store.rec_model.record_feedback('for_you',f'artist:@artist:{a_id}','mute',now=40)
    blocked = store.rec_model.suppressed_keys('for_you',50)
    keys = {r['video_id']:r['identity_key'] for r in store.conn.execute('SELECT * FROM catalog_tracks')}
    assert keys['a'] in blocked and keys['b'] not in blocked


def test_model_uses_credits_and_preserves_user_correction(store):
    one = store.tracks.upsert_track('a','A','AC/DC',None,None,artist_browse_id='one')
    two = store.tracks.upsert_track('b','B','AC DC',None,None,artist_browse_id='two')
    store.tracks.set_track_genre(one,'Rock')
    store.tracks.set_track_genre(two,'Techno')
    first = store.artists.track_credits(one)[0]['artist_id']
    second = store.artists.track_credits(two)[0]['artist_id']
    profiles = artist_model.artist_content_profiles(store)
    assert set(profiles) == {f'@artist:{first}',f'@artist:{second}'}
    assert profiles[f'@artist:{first}']['families'] != profiles[f'@artist:{second}']['families']
    store.tracks.set_track_artist(one,'My correction')
    store.tracks.upsert_track('a','A','Provider credit',None,None,credits=[{'name':'Provider credit','id':'one'}])
    assert store.conn.execute('SELECT artist,orig_artist FROM catalog_tracks WHERE id=?', (one,)).fetchone()[:] == ('My correction','AC/DC')
    assert store.artists.track_credits(one)[0]['artist_id'] == first
    store.tracks.reset_track_artist(one)
    assert store.conn.execute('SELECT artist FROM catalog_tracks WHERE id=?', (one,)).fetchone()[0] == 'AC/DC'
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_artist_conversion_rolls_back_all_ddl_and_preserves_literal_input(artist_legacy_input, tmp_path):
    conn, _ = artist_legacy_input
    before = list(conn.iterdump())

    def interrupted(conn, context):
        MIGRATIONS[20].apply(conn, context)
        raise RuntimeError('interrupted artist migration')

    with pytest.raises(MigrationError, match='interrupted artist'):
        migrate(conn, registry=(*MIGRATIONS[:20], Migration(21, MIGRATIONS[20].name, interrupted)))
    assert list(conn.iterdump()) == before
    assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='artists'").fetchone()
    assert conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 20
    migrate(conn, registry=MIGRATIONS[:21])
    credit, = conn.execute('SELECT credit FROM track_artists')
    assert credit[0] == 'Group, with comma'
    assert not conn.execute('PRAGMA foreign_key_check').fetchone()
    assert not (tmp_path / 'backups').exists()


def assert_literal_artist(store, version):
    credits = store.artists.track_credits(1)
    assert len(credits) == 1 and credits[0]['credit'] == 'Group, with comma'
    aid = credits[0]['artist_id']
    assert store.conn.execute('SELECT artist_id,scanned_at FROM artist_scans').fetchone()[:] == (aid, 15)
    assert store.artist_similarity.cached_similar(aid, 30) == [['Neighbor', .8]]
    assert store.conn.execute('SELECT item_key,kind,artist_id FROM rec_feedback').fetchone()[:] == (
        'Group, with comma', 'less', aid)
    assert store.artists.resolve('Original credit') == aid
    assert store.conn.execute('SELECT artist,orig_artist FROM catalog_tracks').fetchone()[:] == (
        'Group, with comma', 'Original credit')
    credit_id = store.conn.execute('SELECT credit_id FROM artist_credit_observations ORDER BY id LIMIT 1').fetchone()[0]
    assert len(artist_credits.read(store.conn, credit_id).members) == 1
    if version >= 101:
        assert store.conn.execute('SELECT artist_id,shown_at FROM discovery_impressions WHERE artist_id IS NOT NULL').fetchone()[:] == (aid, 6)
        assert store.conn.execute('SELECT artist_id,found_at,first_shown,offered_count,legacy_offered_count FROM discovered_artists').fetchone()[:] == (aid, 10, 6, 1, 4)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
    assert store.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_registered_literal_artist_conversion_preserves_credits_and_feedback(artist_legacy_input, tmp_path):
    conn, path = artist_legacy_input
    migrate(conn, registry=MIGRATIONS[:79], clock=lambda: 1.)
    with catalog_reader(path, 79) as reader:
        assert_literal_artist(reader, 79)
    assert not (tmp_path / 'backups').exists()


@pytest.mark.parametrize('completed_artist_input', [(79, 88), (88, 102), (103, 104), (104, 107)],
                         indirect=True, ids=['metadata', 'subjects', 'domain-contracts', 'native-admission'])
def test_registered_artist_upgrade_keeps_literal_credit_state_and_feedback(completed_artist_input, tmp_path):
    conn, path, source, target = completed_artist_input
    history = list(map(tuple, conn.execute('SELECT version,name FROM schema_migrations ORDER BY version')))
    migrate(conn, registry=MIGRATIONS[:target], clock=lambda: 1.)
    assert list(map(tuple, conn.execute('SELECT version,name FROM schema_migrations ORDER BY version'))) == [
        *history, *[(item.version, item.name) for item in MIGRATIONS[source:target]]]
    with catalog_reader(path, target) as reader:
        assert_literal_artist(reader, target)
    assert not (tmp_path / 'backups').exists()


@pytest.mark.parametrize('completed_artist_input', [(107, 107)], indirect=True, ids=['native-input'])
def test_native_artist_reopen_and_repeated_startup_keep_literal_credits(completed_artist_input, tmp_path):
    conn, path, _, target = completed_artist_input
    history = list(map(tuple, conn.execute('SELECT * FROM schema_migrations ORDER BY version')))
    reopened = Store(path)
    try:
        reopened.init_schema()
        assert_literal_artist(reopened, target)
        before = reopened.artists.track_credits(1)
        reopened.init_schema()
        assert reopened.artists.track_credits(1) == before
        assert list(map(tuple, reopened.conn.execute('SELECT * FROM schema_migrations ORDER BY version'))) == history
        assert not (tmp_path / 'backups').exists()
    finally:
        reopened.conn.close()




@pytest.mark.parametrize('existing_references', ['none','album','both'])
def test_installed_v21_upgrades_references_atomically(
        tmp_path, monkeypatch, existing_references, _pristine_installed_schema):
    from yt_playlist.core.migrations import registry
    from yt_playlist.core.migrations.runner import validate_catalog

    path = tmp_path / 'installed.db'
    s = Store(path)
    _pristine_installed_schema(21).backup(s.conn)
    aid = s.conn.execute("INSERT INTO artists(name,provisional) VALUES ('Artist',1)").lastrowid
    s.conn.execute("INSERT INTO artist_aliases VALUES (?, 'Artist', 'legacy_credit')", (aid,))
    s.conn.execute("INSERT INTO discovered_albums(browse_id,artist,title,found_at) VALUES ('release','Artist','Album',1)")
    s.conn.execute("INSERT INTO rec_feedback(surface,item_key,kind,created_at) VALUES ('artist','Artist','mute',2)")
    s.conn.execute("INSERT INTO rec_feedback(surface,item_key,kind,created_at) VALUES ('for_you','song|artist','less',3)")
    s.conn.execute("INSERT INTO discovery_visible_impressions VALUES ('page','artist','Artist',4)")
    if existing_references != 'none':
        s.conn.execute('ALTER TABLE discovered_albums ADD COLUMN artist_id INTEGER REFERENCES artists(id)')
        s.conn.execute('CREATE INDEX ix_discovered_album_artist ON discovered_albums(artist_id)')
        s.conn.execute('UPDATE discovered_albums SET artist_id=?', (aid,))
    if existing_references == 'both':
        s.conn.execute('ALTER TABLE rec_feedback ADD COLUMN artist_id INTEGER REFERENCES artists(id)')
        s.conn.execute('CREATE INDEX ix_rec_feedback_artist ON rec_feedback(artist_id,kind)')
        s.conn.execute("UPDATE rec_feedback SET artist_id=?,target_type='artist' WHERE surface='artist'", (aid,))
        s.conn.execute("UPDATE discovery_visible_impressions SET item_id=?", (str(aid),))
    s.conn.commit()
    validate_catalog(s.conn,21)

    def interrupted(conn, context):
        MIGRATIONS[21].apply(conn,context)
        raise RuntimeError('interrupted reference migration')

    monkeypatch.setattr(registry,'MIGRATIONS',(*MIGRATIONS[:21],Migration(22,MIGRATIONS[21].name,interrupted)))
    before = list(s.conn.iterdump())
    with pytest.raises(MigrationError,match='interrupted reference'):
        migrate(s.conn, registry=registry.MIGRATIONS)
    assert list(s.conn.iterdump()) == before
    monkeypatch.setattr(registry,'MIGRATIONS',MIGRATIONS)
    s.conn.close()
    s = Store(path)
    # Each installed draft needs its own real v22 rollback/reopen/retry. Later
    # credit conversion and current Store admission belong to the independent
    # populated upgrade owners above, avoiding three more full suffix replays.
    migrate(s.conn, registry=MIGRATIONS[:22])
    assert s.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 22
    assert s.conn.execute('SELECT artist_id FROM discovered_albums').fetchone()[0] == aid
    assert s.conn.execute("SELECT item_key,kind,artist_id,target_type FROM rec_feedback WHERE surface='artist'").fetchone()[:] == (
        'Artist','mute',aid,'artist')
    assert s.conn.execute("SELECT item_key,kind,artist_id FROM rec_feedback WHERE surface='for_you'").fetchone()[:] == (
        'song|artist','less',None)
    assert s.conn.execute("SELECT item_id,shown_at FROM discovery_visible_impressions WHERE kind='artist'").fetchone()[:] == (str(aid),4)
    validate_catalog(s.conn, 22)
    assert not s.conn.execute('PRAGMA foreign_key_check').fetchall()
    before = list(s.conn.iterdump())
    migrate(s.conn, registry=MIGRATIONS[:22])
    assert list(s.conn.iterdump()) == before
    assert not (tmp_path / 'backups').exists()
    assert s.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    s.conn.close()
