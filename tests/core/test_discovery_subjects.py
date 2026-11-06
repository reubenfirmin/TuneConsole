"""Discovery identity, lifetime evidence and file ownership at the typed cutover."""
import sqlite3

import pytest

from yt_playlist.core import artists, collection_recovery, discovery_provenance as provenance, genre_identity
from yt_playlist.core.migrations import current, registry, Migration, MigrationError, migrate
from yt_playlist.core.migrations import v101_discovery_subjects
from yt_playlist.core.store import Store


def discover(store,video='song',source='discovery',now=1):
    return store.discovery.upsert_discovered_track(video+'|artist',video,video,'Artist',None,None,
        None,None,source,now)


def test_fresh_typed_constraints_and_unknown_sources_admit_no_catalog_subjects(store):
    for table in ('artist_scans','discovered_artists'):
        columns = {r[1]:r for r in store.conn.execute('PRAGMA table_info('+table+')')}
        assert 'artist' not in columns and columns['artist_id'][5]==1
    assert not {'because','fits'} & {r[1] for r in store.conn.execute('PRAGMA table_info(discovered_artists)')}
    key = discover(store,source='radio:never-admitted')
    before = store.conn.execute('SELECT count(*) FROM recordings').fetchone()[0]
    source = store.discovery.discovery_sources_for_track(key)[0]
    assert source['source_type']=='legacy_unknown' and source['resolution_status']=='unresolved'
    assert source['source_token']=='radio:never-admitted'
    assert store.conn.execute('SELECT count(*) FROM recordings').fetchone()[0]==before
    assert not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='never-admitted'").fetchone()
    rid = source['recording_id']
    aid = store.artists.ensure_artist('Artist')
    for sql,args in (
        ("INSERT INTO discovery_origins(kind,resolution_status) VALUES ('radio','resolved')",()),
        ("INSERT INTO discovery_origins(kind,seed_video_id,resolution_status) VALUES ('album','song','resolved')",()),
        ('INSERT INTO artist_scans VALUES (?,0)',(99999,)),
        ('INSERT INTO discovery_impressions(view_id,recording_id,artist_id,shown_at) VALUES (?,?,?,0)',('view',rid,aid)),
        ('INSERT INTO discovery_impressions(view_id,recording_id,shown_at) VALUES (?,99999,0)',('view',)),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(sql,args)
    store.conn.rollback()
    current.validate(store.conn)


def test_distinct_exact_upload_contributions_and_unknown_input_do_not_overwrite(store):
    first = store.tracks.upsert_track('a','A','Artist',None,10)
    store.tracks.upsert_track('b','B','Artist',None,10)
    rid = store.tracks.catalog_subject(first)['recording_id']
    store.tracks.associate_youtube_item('b',rid,source='verified',evidence={'review':'same recording'},now=1)
    for video,when in (('a',2),('b',3),('a',4)):
        discover(store,video,'radio:a',when)
    with store.conn:
        oid = provenance.origin(store.conn,'radio:a')
        provenance.retain(store.conn,rid,oid,None,5,first_found_at=None)
        provenance.retain(store.conn,rid,oid,None,6,first_found_at=None)
    rows = store.conn.execute('SELECT input_video_id,first_found_at,last_found_at FROM discovery_sources ORDER BY id').fetchall()
    assert list(map(tuple,rows))==[('a',2,4),('b',3,3),(None,None,6)]
    assert store.conn.execute("SELECT seed_video_id FROM discovery_origins WHERE kind='radio'").fetchone()[0]=='a'


@pytest.mark.parametrize('kind',['track','album','artist'])
def test_views_survive_expiry_rediscovery_and_old_visit_retries(store,kind):
    def add():
        if kind=='track':
            return discover(store)
        if kind=='album':
            store.discovery.upsert_discovered_album('release','Artist','Release',None,None,1)
            return 'release'
        store.discovery.upsert_discovered_artist('Candidate',1,[],[],None,1)
        return 'Candidate'
    key = add()
    store.discovery.record_discovery_impressions(kind,[key,key],'visit',2)
    store.discovery.gc_discovery_pool(200*86400,30,set())
    assert store.conn.execute('SELECT count(*) FROM discovery_impressions').fetchone()[0]==1
    add()
    store.discovery.record_discovery_impressions(kind,[key],'visit',200*86400)
    table,column = provenance.CANDIDATES[kind]
    identifier = provenance.subject(store.conn,kind,key)
    assert tuple(store.conn.execute('SELECT offered_count,first_shown,last_shown FROM '+table+
        ' WHERE '+column+'=?',(identifier,)).fetchone())==(1,2,2)
    store.discovery.record_discovery_impressions(kind,[key],'other-visit',200*86400)
    assert store.conn.execute('SELECT offered_count FROM '+table+' WHERE '+column+'=?',(identifier,)).fetchone()[0]==2


def test_account_and_import_withdrawal_preserves_independent_sources_and_lifetime_views(store):
    first = store.accounts.upsert_account('First','credential',None,True)
    second = store.accounts.upsert_account('Second','other',None,False)
    store.discovery.upsert_discovered_artist('Candidate',2,['Independent'],['Playlist'],None,1)
    store.discovery.seed_spotify_artists(first,{'Candidate':3},2,import_key='one')
    store.discovery.seed_spotify_artists(first,{'Candidate':5},3,import_key='two')
    store.discovery.seed_spotify_artists(second,{'Candidate':7},4,import_key='one')
    store.discovery.record_discovery_impressions('artist',['Candidate'],'visit',5)
    assert store.conn.execute('SELECT count(*) FROM discovery_artist_sources').fetchone()[0]==4
    with store.conn:
        assert provenance.remove_spotify_artists(store.conn,account_id=first,import_key='one')==0
    assert store.conn.execute('SELECT count(*) FROM discovery_artist_sources').fetchone()[0]==3
    candidate = store.discovery.get_discovered_artists()[0]
    assert candidate['because']==['Independent'] and candidate['fits']==['Playlist'] and candidate['offered_count']==1
    with store.conn:
        provenance.remove_spotify_artists(store.conn,account_id=first)
    assert store.conn.execute('SELECT count(*) FROM discovery_artist_sources').fetchone()[0]==2
    with store.conn:
        provenance.remove_spotify_artists(store.conn,account_id=second)
    candidate = store.discovery.get_discovered_artists()[0]
    assert candidate['offered_count']==1
    assert candidate['because']==['Independent'] and candidate['fits']==['Playlist']
    assert store.conn.execute('SELECT count(*) FROM discovery_artist_sources').fetchone()[0]==1


def installed_100(path):
    store = Store(path)
    for sql in current.statements(version=100):
        store.conn.execute(sql)
    genre_identity.seed_catalog(store.conn)
    store.conn.executemany('INSERT INTO settings VALUES (?,?)',current.SEED_SETTINGS.items())
    store.conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',[(m.version,m.name) for m in registry.MIGRATIONS[:100]])
    store.conn.commit()
    store._bind_repositories()
    return store


def test_populated_conversion_rollback_unknown_histories_and_legacy_precision(tmp_path):
    store = installed_100(tmp_path/'old.db')
    aid = artists.ensure(store.conn,'Candidate')
    store.conn.execute('INSERT INTO artist_scans VALUES (?,NULL,?)',('@artist:'+str(aid),aid))
    store.conn.execute('INSERT INTO discovered_artists(artist,artist_id,because,fits,found_at,offered_count,first_shown,'
        "last_shown,legacy_offered_count,legacy_first_shown,legacy_last_shown) VALUES (?,?,'[\"Anchor\"]','[]',1,2,3,5,8,0,2)",('@artist:'+str(aid),aid))
    store.conn.execute('INSERT INTO discovery_artist_sources VALUES (?,\'unknown\',NULL,1)',(aid,))
    store.conn.execute("INSERT INTO discovery_visible_impressions VALUES ('visit','artist',?,3)",(str(aid),))
    store.conn.execute("INSERT INTO discovery_visible_impressions VALUES ('missing','album','absent',4)")
    store.conn.commit()
    before = list(store.conn.iterdump())
    def interrupted(conn,context):
        v101_discovery_subjects.apply(conn,context)
        raise RuntimeError('interrupted discovery cutover')
    with pytest.raises(MigrationError,match='interrupted discovery cutover'):
        migrate(store.conn,registry=(*registry.MIGRATIONS[:100],Migration(101,'discovery_subjects',interrupted,rebuild_tables=True)))
    assert list(store.conn.iterdump())==before
    # Historical conversion is now an explicit operation. Ordinary startup
    # retains this installed owner instead of implicitly replaying bulk work.
    migrate(store.conn,registry=registry.MIGRATIONS)
    store.init_schema()
    candidate = store.discovery.get_discovered_artists()[0]
    assert candidate['because']==['Anchor'] and candidate['offered_count']==1 and candidate['first_shown']==3
    assert store.conn.execute('SELECT legacy_offered_count FROM discovered_artists').fetchone()[0]==9
    assert tuple(store.conn.execute('SELECT original_key,reason FROM unresolved_discovery_impressions').fetchone())==('absent','missing_owner')
    assert tuple(store.conn.execute('SELECT kind,legacy_source_key,resolution_status FROM discovery_origins').fetchone())==('legacy_unknown','unknown','unresolved')
    assert store.conn.execute('SELECT count(*) FROM artist_scans WHERE scanned_at IS NULL').fetchone()[0]==1
    current.validate(store.conn)
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
    store.conn.close()
    reopened = Store(tmp_path/'old.db')
    reopened.init_schema()
    assert reopened.discovery.get_discovered_artists()[0]['because']==['Anchor']
    reopened.conn.close()


def test_candidate_output_backup_restore_and_orphan_cleanup(tmp_path):
    store = Store(tmp_path/'source.db')
    store.init_schema()
    try:
        store.discovery.upsert_discovered_artist('Candidate',1,['Anchor'],['Playlist'],None,1)
        store.conn.execute('BEGIN')
        with sqlite3.connect(tmp_path/'copy.db') as copied:
            store.conn.backup(copied)
        manifest = collection_recovery.export(store.conn,tmp_path/'backup')
        store.conn.rollback()
        copied = Store(tmp_path/'copy.db')
        try:
            collection_recovery.restore(copied.conn,manifest)
            copied.init_schema()
            assert copied.discovery.get_discovered_artists()[0]['fits']==['Playlist']
            copied.discovery.upsert_discovered_artist('Candidate',2,['Other'],[],None,2)
            assert collection_recovery.collect_orphans(copied.conn)==1
            assert copied.discovery.get_discovered_artists()[0]['because']==['Other']
            with copied.conn:
                copied.conn.execute('DELETE FROM discovered_artists')
            assert collection_recovery.collect_orphans(copied.conn)==1
        finally:
            copied.conn.close()
    finally:
        store.conn.close()
