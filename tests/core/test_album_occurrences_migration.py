"""Release occurrence ownership, legacy uncertainty and atomic publication."""
import json
import sqlite3

import pytest

from yt_playlist.core import albums, album_tracklists, genre_identity
from yt_playlist.core.migrations import Migration, MigrationError, current, migrate, registry
from yt_playlist.core.migrations import v099_album_occurrences
from yt_playlist.core.migrations.runner import validate_version
from yt_playlist.core.store import Store
from yt_playlist.library.album_catalog import remember_album


def installed_98(path):
    store = Store(path)
    try:
        for sql in current.statements(version=98):
            store.conn.execute(sql)
        genre_identity.seed_catalog(store.conn)
        store.conn.executemany('INSERT INTO settings VALUES (?,?)',current.SEED_SETTINGS.items())
        for sql in ('INSERT INTO rec_preparation VALUES (1,0,0)','INSERT INTO rec_group_clock VALUES (1,0)',
                    'INSERT INTO group_retention_state VALUES (1,0)'):
            store.conn.execute(sql)
        store.conn.executemany('INSERT INTO schema_migrations VALUES (?,?,1)',[(m.version,m.name) for m in registry.MIGRATIONS[:98]])
        store.conn.commit()
        store._bind_repositories()
        return store
    except BaseException:
        store.conn.close()
        raise


def response(videos=('one','two','one')):
    return {'title':'Release','artists':[{'name':'Artist'}],'trackCount':len(videos),
            'tracks':[{'title':v,'videoId':v,'discNumber':1 if i==0 else 2,'trackNumber':i+1} for i,v in enumerate(videos)]}


def test_legacy_occurrences_keep_unknown_order_incidental_evidence_and_exact_frozen_shares(tmp_path, legacy_owner_store):
    store = legacy_owner_store(tmp_path/'old.db', 98)
    entry = store.tracks.upsert_track('one','One','Artist',None,10,album_browse_id='release')
    aid = albums.lookup(store.conn,'release')
    with store.conn:
        store.conn.executemany("INSERT INTO album_tracks(album_id,entry_id,position,is_full,source) VALUES (?,?,NULL,1,'legacy')",[(aid,entry),(aid,entry)])
        store.conn.execute('INSERT INTO album_tracks(album_id,legacy_track_key,is_full,source) VALUES (?,?,1,?)',(aid,'unknown|artist','legacy'))
        store.conn.execute('INSERT INTO album_fetch_state VALUES (?,NULL,1,3)',(aid,))
        store.conn.execute('INSERT INTO album_share_snapshots VALUES (?,?,?)',('release','Frozen',json.dumps(['one','missing','one'])))
    original = {r['id']:dict(r) for r in store.conn.execute('SELECT * FROM album_tracks')}
    migrate(store.conn, registry=registry.MIGRATIONS)
    store.init_schema()
    assert store.conn.execute('SELECT current_tracklist_id FROM albums WHERE id=?',(aid,)).fetchone()[0] is not None
    full = store.conn.execute("SELECT * FROM album_tracklists WHERE capture_kind='legacy_full'").fetchone()
    assert (full['status'],full['order_status'],full['fetched_at'])==('complete','unknown',None)
    captured = list(store.conn.execute('SELECT * FROM album_track_occurrences WHERE tracklist_id=?',(full['id'],)))
    assert len(captured)==3 and all(r['position'] is None for r in captured)
    assert {r['legacy_row_id'] for r in captured}=={i for i,r in original.items() if r['is_full']}
    assert store.conn.execute('SELECT count(*) FROM album_item_observations').fetchone()[0]==1
    frozen = store.conn.execute('SELECT t.capture_kind,t.status,t.order_status FROM album_share_snapshots s JOIN album_tracklists t ON t.id=s.tracklist_id').fetchone()
    assert tuple(frozen)==('legacy_share','snapshot','known')
    assert [r[0] for r in store.conn.execute('SELECT raw_source_ref FROM album_share_items ORDER BY position')]==['one','missing','one']
    missing = store.conn.execute("SELECT * FROM album_track_occurrences WHERE raw_source_ref='missing'").fetchone()
    assert missing['recording_id'] is None and missing['resolution_status']=='unresolved'
    assert not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='missing'").fetchone()
    assert store.conn.execute('SELECT count(*) FROM catalog_entries').fetchone()[0]==1
    assert store.collection.cached_album_share('release')=={'title':'Frozen','video_ids':['one','missing','one']}
    current.validate(store.conn)
    store.conn.close()
    reopened = Store(tmp_path/'old.db')
    try:
        reopened.init_schema()
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
        assert reopened.conn.execute('SELECT count(*) FROM album_track_occurrences').fetchone()[0]==6
    finally:
        reopened.conn.close()


def test_partial_refresh_retains_last_good_and_frozen_share_while_ordinary_queue_follows_current(store):
    remember_album(store,'release',response())
    with store.conn:
        store.conn.execute("INSERT OR IGNORE INTO library_memberships(entry_id,origin) SELECT id,'fixture' FROM catalog_entries")
    aid = albums.lookup(store.conn,'release')
    first = store.conn.execute('SELECT current_tracklist_id FROM albums WHERE id=?',(aid,)).fetchone()[0]
    assert store.collection.cached_album_share('release')['video_ids']==['one','two','one']
    partial = response(('two',))
    partial['trackCount']=3
    remember_album(store,'release',partial)
    assert store.conn.execute('SELECT current_tracklist_id FROM albums WHERE id=?',(aid,)).fetchone()[0]==first
    assert store.collection.cached_album_share('release')['video_ids']==['one','two','one']
    assert store.conn.execute("SELECT count(*) FROM album_tracklists WHERE status='partial'").fetchone()[0]==1
    remember_album(store,'new-partial',partial)
    assert 'new-partial' not in store.collection.cached_album_tracklist_ids()
    assert store.conn.execute("SELECT current_tracklist_id FROM albums WHERE id=?",(albums.lookup(store.conn,'new-partial'),)).fetchone()[0] is None
    remember_album(store,'release',response(('two','one')))
    assert store.collection.cached_album_share('release')['video_ids']==['two','one']
    with store.conn:
        snapshot = album_tracklists.freeze(store.conn,aid,'release','Frozen',['one','one','two'],now=10)
    remember_album(store,'release',response(('one','two')))
    assert store.collection.cached_album_share('release')=={'title':'Frozen','video_ids':['one','one','two']}
    assert store.conn.execute('SELECT count(*) FROM album_share_items WHERE snapshot_id=?',(snapshot,)).fetchone()[0]==3
    assert store.conn.execute('SELECT count(*) FROM album_track_occurrences WHERE tracklist_id=?',(first,)).fetchone()[0]==3


def test_owner_source_count_and_frozen_capture_guards(store):
    remember_album(store,'release',response())
    remember_album(store,'other',response(('other',)))
    aid = albums.lookup(store.conn,'release')
    other = albums.lookup(store.conn,'other')
    tracklist = store.conn.execute('SELECT current_tracklist_id FROM albums WHERE id=?',(aid,)).fetchone()[0]
    occurrence = store.conn.execute('SELECT id FROM album_track_occurrences WHERE tracklist_id=? LIMIT 1',(tracklist,)).fetchone()[0]
    wrong = store.conn.execute("SELECT id FROM catalog_entries WHERE video_id='other'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError,match='complete owned publication'):
        with store.conn:
            store.conn.execute('UPDATE albums SET current_tracklist_id=? WHERE id=?',(tracklist,other))
    with pytest.raises(sqlite3.IntegrityError,match='disagrees'):
        with store.conn:
            store.conn.execute("INSERT INTO album_occurrence_sources VALUES (?,?,'youtube')",(occurrence,wrong))
    with pytest.raises(sqlite3.IntegrityError,match='capture cannot change'):
        with store.conn:
            store.conn.execute('UPDATE album_track_occurrences SET position=20 WHERE id=?',(occurrence,))
    with pytest.raises(sqlite3.IntegrityError,match='incomplete'):
        with store.conn:
            building = album_tracklists.begin(store.conn,aid,'release',expected_count=2)
            album_tracklists.occurrence(store.conn,building,entry_id=wrong,position=0)
            album_tracklists.finish(store.conn,building,complete=True)
    with pytest.raises(sqlite3.OperationalError,match='view'):
        store.conn.execute('DELETE FROM album_tracks')
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_interruption_rolls_back_owner_replacement_then_restart_preserves_it(tmp_path,monkeypatch,legacy_owner_store):
    store = legacy_owner_store(tmp_path/'rollback.db', 98)
    store.tracks.upsert_track('one','One','Artist',None,10,album_browse_id='release')
    before = [dict(r) for r in store.conn.execute('SELECT * FROM album_tracks')]
    original_dump = list(store.conn.iterdump())
    def interrupt(conn,context):
        v099_album_occurrences.apply(conn,context)
        raise RuntimeError('interrupted occurrence cutover')
    original = registry.MIGRATIONS
    monkeypatch.setattr(registry,'MIGRATIONS',(*original[:98],Migration(99,'album_occurrences',interrupt,rebuild_tables=True)))
    with pytest.raises(MigrationError,match='interrupted occurrence cutover'):
        migrate(store.conn, registry=registry.MIGRATIONS)
    assert list(store.conn.iterdump()) == original_dump
    assert [dict(r) for r in store.conn.execute('SELECT * FROM album_tracks')]==before
    assert store.conn.execute("SELECT type FROM sqlite_schema WHERE name='album_tracks'").fetchone()[0]=='table'
    monkeypatch.setattr(registry,'MIGRATIONS',original)
    # Retry the owning v99 conversion after a real disk reopen. The populated
    # legacy-occurrence case above owns the full suffix and current admission.
    path = store._db_path
    store.conn.close()
    reopened = Store(path)
    try:
        migrate(reopened.conn, registry=original[:99])
        assert reopened.conn.execute('SELECT legacy_row_id FROM album_item_observations').fetchone()[0]==before[0]['id']
        assert reopened.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 99
        validate_version(reopened.conn, 99)
        assert not reopened.conn.execute('PRAGMA foreign_key_check').fetchall()
        assert reopened.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        reopened.conn.close()


def test_interrupted_publication_keeps_old_pointer_and_captured_order(store,monkeypatch):
    remember_album(store,'release',response())
    with store.conn:
        store.conn.execute("INSERT OR IGNORE INTO library_memberships(entry_id,origin) SELECT id,'fixture' FROM catalog_entries")
    aid = albums.lookup(store.conn,'release')
    previous = store.conn.execute('SELECT current_tracklist_id FROM albums WHERE id=?',(aid,)).fetchone()[0]
    count = store.conn.execute('SELECT count(*) FROM album_tracklists').fetchone()[0]
    finish = album_tracklists.finish
    def interrupted(conn,tracklist_id,*,complete):
        finish(conn,tracklist_id,complete=complete)
        raise RuntimeError('lost publication')
    monkeypatch.setattr(album_tracklists,'finish',interrupted)
    with pytest.raises(RuntimeError,match='lost publication'):
        remember_album(store,'release',response(('two','one')))
    assert store.conn.execute('SELECT current_tracklist_id FROM albums WHERE id=?',(aid,)).fetchone()[0]==previous
    assert store.conn.execute('SELECT count(*) FROM album_tracklists').fetchone()[0]==count
    assert store.collection.cached_album_share('release')['video_ids']==['one','two','one']


def test_reassociation_updates_resolution_without_rewriting_occurrence_capture(store):
    remember_album(store,'release',response())
    old = store.conn.execute("SELECT recording_id FROM catalog_entries WHERE video_id='one'").fetchone()[0]
    replacement = store.tracks.upsert_track('new','Different title','Other artist',None,10)
    new = store.tracks.catalog_subject(replacement)['recording_id']
    before = [dict(r) for r in store.conn.execute('SELECT * FROM album_track_occurrences WHERE recording_id=?',(old,))]
    store.tracks.associate_youtube_item('one',new,source='verified',evidence={'same_recording':True},now=10)
    for original in before:
        row = dict(store.conn.execute('SELECT * FROM album_track_occurrences WHERE id=?',(original['id'],)).fetchone())
        assert row=={**original,'recording_id':new}
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_incidental_association_cannot_expand_published_denominator(store):
    remember_album(store,'release',response())
    bonus = store.tracks.upsert_track('bonus','Bonus','Artist',None,10,album_browse_id='release')
    aid = albums.lookup(store.conn,'release')
    assert store.conn.execute('SELECT 1 FROM album_item_observations WHERE album_id=? AND entry_id=?',(aid,bonus)).fetchone()
    context, = store.collection.taste_album_contexts(include_browse=['release'])
    assert len(context['keys'])==2 and context['tracklist_complete']
    assert store.collection.album_listening_summary(['release'])['release']['tracks']==2


def test_unreferenced_provider_revisions_expire_but_frozen_occurrences_survive(store):
    remember_album(store,'release',response())
    aid = albums.lookup(store.conn,'release')
    first = store.conn.execute('SELECT current_tracklist_id FROM albums WHERE id=?',(aid,)).fetchone()[0]
    with store.conn:
        snapshot = album_tracklists.freeze(store.conn,aid,'release','First',['one','two','one'],now=1)
    assert store.conn.execute('SELECT tracklist_id FROM album_share_snapshots WHERE id=?',(snapshot,)).fetchone()[0]==first
    for i in range(8):
        remember_album(store,'release',response(('one','two') if i%2 else ('two','one')))
    assert store.conn.execute('SELECT count(*) FROM album_tracklists').fetchone()[0]==4
    assert store.conn.execute('SELECT count(*) FROM album_track_occurrences WHERE tracklist_id=?',(first,)).fetchone()[0]==3
    assert album_tracklists.share(store.conn,aid,'release')['video_ids']==['one','two','one']
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_publication_collects_only_its_release_with_bounded_query_work(store):
    from yt_playlist.core.activity import interrupt_query

    def publish():
        work = 0
        def progress():
            nonlocal work
            work += 100
            return 0
        store.conn.set_progress_handler(progress, 100)
        try:
            remember_album(store, 'release', response())
        finally:
            store.conn.set_progress_handler(interrupt_query, 1000)
        return work

    small = publish()
    aid = albums.lookup(store.conn, 'release')
    with store.conn:
        frozen = album_tracklists.freeze(store.conn, aid, 'release', 'Frozen', ['one','two','one'], now=1)
        # Retained histories deliberately exceed keep. An unrelated publication must leave them
        # alone; publication-time cleanup is not a global maintenance sweep.
        for i in range(400):
            provider_id = f'unrelated-{i}'
            owner = albums.observe(store.conn, provider_id, title=provider_id)
            for _ in range(6):
                revision = album_tracklists.begin(store.conn, owner, provider_id)
                album_tracklists.finish(store.conn, revision, complete=False)
    untouched = [row[0] for row in store.conn.execute(
        "SELECT id FROM album_tracklists WHERE provider_id<>'release' ORDER BY id")]
    work = [publish() for _ in range(7)]
    assert max(work) < small * 2 + 5000, (small, work)
    assert [row[0] for row in store.conn.execute(
        "SELECT id FROM album_tracklists WHERE provider_id<>'release' ORDER BY id")] == untouched
    assert store.conn.execute("SELECT count(*) FROM album_tracklists WHERE provider_id='release'").fetchone()[0] == 4
    partial = response(('one',))
    partial['trackCount'] = 2
    for _ in range(5):
        remember_album(store, 'release', partial)
    assert dict(store.conn.execute("SELECT status,count(*) FROM album_tracklists WHERE provider_id='release' GROUP BY status")) == {
        'complete': 4, 'partial': 2}
    assert store.conn.execute('SELECT tracklist_id FROM album_share_snapshots WHERE id=?', (frozen,)).fetchone()
    assert album_tracklists.share(store.conn, aid, 'release')['video_ids'] == ['one','two','one']
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()


def test_unavailable_placeholder_retains_position_without_fabricating_a_recording(store):
    payload = response(('one','two'))
    payload['tracks'][1] = {'videoId':'unavailable','discNumber':2,'trackNumber':1}
    remember_album(store,'release',payload)
    aid = albums.lookup(store.conn,'release')
    row = store.conn.execute('SELECT o.* FROM albums a JOIN album_track_occurrences o '
        'ON o.tracklist_id=a.current_tracklist_id WHERE a.id=? AND o.position=1',(aid,)).fetchone()
    assert row['recording_id'] is None and row['resolution_status']=='unavailable'
    assert (row['raw_source_ref'],row['disc_number'],row['track_number'])==('unavailable',2,1)
    assert not store.conn.execute("SELECT 1 FROM youtube_items WHERE video_id='unavailable'").fetchone()
    assert store.collection.cached_album_share('release') is None
