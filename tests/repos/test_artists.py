import json

import pytest

from tests.conftest import FakeClient
from yt_playlist.core.migrations import Migration, MigrationError, migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.core.store import Store
from yt_playlist.library.album_catalog import remember_album
from yt_playlist.library.sync import refresh_playlist
from yt_playlist.rec import artist_model


def test_playlist_and_album_import_keep_all_ordered_credits(store):
    identity = store.identities.upsert_identity('main','credential',None,True)
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


def test_populated_upgrade_retains_literal_credit_state_and_feedback_without_backups(tmp_path):
    s = Store(tmp_path / 'state.db')
    migrate(s.conn,registry=MIGRATIONS[:20])
    s.conn.execute("INSERT INTO recordings(id,identity_key) VALUES (1,'song|credit')")
    s.conn.execute("INSERT INTO youtube_items(video_id,recording_id,artist,orig_artist) VALUES ('v',1,'Group, with comma','Original credit')")
    s.conn.execute("INSERT INTO catalog_entries(id,recording_id,video_id) VALUES (1,1,'v')")
    s.conn.execute("INSERT INTO artist_scans VALUES ('Group, with comma',15)")
    s.conn.execute("INSERT INTO rec_artist_similar VALUES ('group, with comma','[[\"Neighbor\",0.8]]',20)")
    s.conn.execute("INSERT INTO rec_feedback(surface,item_key,kind,created_at) VALUES ('artist','Group, with comma','less',5)")
    s.conn.execute("INSERT INTO discovery_visible_impressions VALUES ('page','artist','Group, with comma',6)")
    s.conn.execute("INSERT INTO discovered_artists(artist,found_at,first_shown,offered_count) VALUES ('Group, with comma',10,50,3)")
    s.conn.execute("INSERT INTO discovered_artists(artist,found_at,first_shown,offered_count) VALUES ('Original credit',20,40,2)")
    s.conn.execute("INSERT INTO entry_field_choices VALUES (1,'artist','\"Group, with comma\"','user')")
    s.conn.commit()

    def interrupted(conn, context):
        MIGRATIONS[20].apply(conn,context)
        raise RuntimeError('interrupted artist migration')

    with pytest.raises(MigrationError,match='interrupted artist'):
        migrate(s.conn,registry=(*MIGRATIONS[:20],Migration(21,MIGRATIONS[20].name,interrupted)))
    assert not s.conn.execute("SELECT 1 FROM sqlite_master WHERE name='artists'").fetchone()
    assert s.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 20
    s.init_schema()
    credits = s.artists.track_credits(1)
    assert len(credits) == 1 and credits[0]['credit'] == 'Group, with comma'
    aid = credits[0]['artist_id']
    assert s.conn.execute('SELECT artist_id,scanned_at FROM artist_scans').fetchone()[:] == (aid,15)
    assert s.artist_similarity.cached_similar(aid,30) == [['Neighbor',.8]]
    assert s.conn.execute('SELECT item_key,kind FROM rec_feedback').fetchone()[:] == ('Group, with comma','less')
    assert s.conn.execute('SELECT artist_id FROM rec_feedback').fetchone()[0] == aid
    assert s.artists.resolve('Original credit') == aid
    assert s.conn.execute("SELECT item_id,shown_at FROM discovery_visible_impressions WHERE kind='artist'").fetchone()[:] == (str(aid),6)
    assert s.conn.execute('SELECT artist_id,found_at,first_shown,offered_count FROM discovered_artists').fetchone()[:] == (aid,10,40,5)
    assert s.conn.execute('SELECT artist,orig_artist FROM catalog_tracks').fetchone()[:] == ('Group, with comma','Original credit')
    assert len(json.loads(s.conn.execute('SELECT credits_json FROM artist_credit_observations').fetchone()[0])) == 1
    before = s.artists.track_credits(1)
    s.init_schema()
    assert s.artists.track_credits(1) == before
    assert not s.conn.execute('PRAGMA foreign_key_check').fetchall()
    assert not (tmp_path / 'backups').exists()
    assert s.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.mark.parametrize('existing_references', ['none','album','both'])
def test_installed_v21_upgrades_references_atomically(tmp_path, monkeypatch, existing_references):
    from yt_playlist.core.migrations import registry
    from yt_playlist.core.migrations.runner import validate_catalog

    path = tmp_path / 'installed.db'
    s = Store(path)
    migrate(s.conn,registry=MIGRATIONS[:21])
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
        migrate(s.conn)
    assert list(s.conn.iterdump()) == before
    monkeypatch.setattr(registry,'MIGRATIONS',MIGRATIONS)
    s.conn.close()
    s = Store(path)
    s.init_schema()
    assert s.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == MIGRATIONS[-1].version
    assert s.conn.execute('SELECT artist_id FROM discovered_albums').fetchone()[0] == aid
    assert s.conn.execute("SELECT item_key,kind,artist_id,target_type FROM rec_feedback WHERE surface='artist'").fetchone()[:] == (
        'Artist','mute',aid,'artist')
    assert s.conn.execute("SELECT item_key,kind,artist_id FROM rec_feedback WHERE surface='for_you'").fetchone()[:] == (
        'song|artist','less',None)
    assert s.conn.execute("SELECT item_id,shown_at FROM discovery_visible_impressions WHERE kind='artist'").fetchone()[:] == (str(aid),4)
    assert not s.conn.execute('PRAGMA foreign_key_check').fetchall()
    before = list(s.conn.iterdump())
    s.init_schema()
    assert list(s.conn.iterdump()) == before
    assert not (tmp_path / 'backups').exists()
    assert s.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
