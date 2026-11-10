"""Opt-in release conversion on a retained actual-library copy, without transport."""
import json
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core import album_tracklists, collection_recovery, policy_documents
from yt_playlist.core.migrations import current, registry
from yt_playlist.core.store import Store


def storage(conn,tables):
    names = [r[0] for r in conn.execute('SELECT name FROM sqlite_schema WHERE tbl_name IN ('+
        ','.join('?' for _ in tables)+')',tables)]
    return conn.execute('SELECT coalesce(sum(pgsize),0) FROM dbstat WHERE name IN ('+
        ','.join('?' for _ in names)+')',names).fetchone()[0]


def test_actual_album_occurrences_share_order_refresh_and_restart(request,tmp_path,monkeypatch):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply a retained isolated actual-library donor at schema 95')
    source = Path(option).resolve(strict=True)
    assert source.is_relative_to(Path(request.config.rootpath).resolve()/'.pytest_cache')
    target = tmp_path/'release-occurrences.db'
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as donor:
        assert donor.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]==95
        with policy_documents.retained_reader(collection_recovery.directory(donor)):
            donor.execute('BEGIN')
            with sqlite3.connect(target) as copied:
                donor.backup(copied)
            manifest = collection_recovery.export(donor,tmp_path/'collection-backup')
            donor.rollback()
    store = Store(target)
    try:
        collection_recovery.restore(store.conn,manifest)
        store._bind_repositories()
        original_statements = current.statements
        original_registry = registry.MIGRATIONS
        with monkeypatch.context() as old:
            old.setattr(current,'VERSION',98)
            old.setattr(current,'statements',lambda *,version=98:original_statements(version=version))
            old.setattr(registry,'MIGRATIONS',original_registry[:98])
            current.contract.cache_clear()
            store.init_schema()
        current.contract.cache_clear()
        original = {r['id']:dict(r) for r in store.conn.execute('SELECT * FROM album_tracks')}
        states = {r['album_id']:dict(r) for r in store.conn.execute('SELECT * FROM album_fetch_state')}
        shares = {r['browse_id']:(r['title'],json.loads(r['video_ids_json'])) for r in store.conn.execute('SELECT * FROM album_share_snapshots')}
        assert len(original)>13000 and len(shares)>=24
        protected = ('albums','album_provider_ids','account_albums','unattributed_album_memberships','recordings',
            'catalog_entries','actions','metadata_observations','metadata_conflicts','predictions','prediction_inputs','recommendation_pools')
        def counts():
            return {table:store.conn.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in protected}
        before = counts()
        size_before = storage(store.conn,('albums','album_tracks','album_fetch_state','album_share_snapshots'))
        started = time.monotonic()
        store.init_schema()
        elapsed = time.monotonic()-started
        assert counts()==before
        incidental = {r['legacy_row_id']:dict(r) for r in store.conn.execute('SELECT * FROM album_item_observations WHERE legacy_row_id IS NOT NULL')}
        full = {r['legacy_row_id']:dict(r) for r in store.conn.execute('SELECT o.*,t.album_id,'
            '(SELECT min(s.entry_id) FROM album_occurrence_sources s WHERE s.occurrence_id=o.id) entry_id '
            'FROM album_track_occurrences o JOIN album_tracklists t ON t.id=o.tracklist_id WHERE o.legacy_row_id IS NOT NULL')}
        assert set(original)==set(incidental)|set(full)
        for key,row in original.items():
            migrated = full[key] if row['is_full'] else incidental[key]
            assert (migrated['album_id'],migrated['entry_id'],migrated['raw_identity_key'])==(
                row['album_id'],row['entry_id'],row['legacy_track_key'])
            assert migrated['captured_source' if row['is_full'] else 'provider']==row['source']
            if row['entry_id'] is None:
                assert migrated['recording_id']==row['recording_id']
            for column in ('position','disc_number','track_number'):
                assert migrated[column if row['is_full'] else 'captured_'+column]==row[column]
        upgraded = {}
        for row in store.conn.execute('SELECT * FROM album_share_snapshots'):
            upgraded[row['captured_provider_id']] = (row['title'],[r[0] for r in store.conn.execute(
                'SELECT raw_source_ref FROM album_share_items WHERE snapshot_id=? ORDER BY position',(row['id'],))])
        assert upgraded==shares
        for aid,state in states.items():
            tracklist = store.conn.execute("SELECT * FROM album_tracklists WHERE album_id=? AND capture_kind='legacy_full'",(aid,)).fetchone()
            assert tracklist is not None
            assert (tracklist['fetched_at'],tracklist['expected_count'])==(state['fetched_at'],state['expected_track_count'])
        current.validate(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        size_after = storage(store.conn,('albums','album_tracklists','album_track_occurrences','album_occurrence_sources',
            'album_item_observations','album_share_snapshots','album_share_items'))
        report = {'legacy_rows':len(original),'incidental':len(incidental),'full_occurrences':len(full),'shares':len(shares),
            'unknown_positions':sum(r['position'] is None for r in original.values() if r['is_full']),
            'protected':before,'sql_bytes_before':size_before,'sql_bytes_after':size_after,'new_payload_file_bytes':0,
            'upgrade_seconds':elapsed,'sqlite':sqlite3.sqlite_version}
        # A controlled interrupted refresh on this actual release cannot replace
        # its useful publication. These calls use local SQL only.
        published = store.conn.execute("SELECT a.id,a.current_tracklist_id,t.provider_id FROM albums a "
            "JOIN album_tracklists t ON t.id=a.current_tracklist_id WHERE t.order_status='known' LIMIT 1").fetchone()
        assert published is not None
        source_entry = store.conn.execute('SELECT s.entry_id FROM album_track_occurrences o JOIN album_occurrence_sources s ON s.occurrence_id=o.id '
            'WHERE o.tracklist_id=? LIMIT 1',(published['current_tracklist_id'],)).fetchone()[0]
        with store.conn:
            partial = album_tracklists.begin(store.conn,published['id'],published['provider_id'],expected_count=99)
            album_tracklists.occurrence(store.conn,partial,entry_id=source_entry,position=0)
            album_tracklists.finish(store.conn,partial,complete=False)
        assert store.conn.execute('SELECT current_tracklist_id FROM albums WHERE id=?',(published['id'],)).fetchone()[0]==published['current_tracklist_id']
        with pytest.raises(RuntimeError,match='interrupted populated publication'):
            with store.conn:
                pending = album_tracklists.begin(store.conn,published['id'],published['provider_id'],expected_count=1)
                album_tracklists.occurrence(store.conn,pending,entry_id=source_entry,position=0)
                album_tracklists.finish(store.conn,pending,complete=True)
                raise RuntimeError('interrupted populated publication')
        store.conn.close()
        store = Store(target)
        store.init_schema()
        assert counts()==before
        assert store.conn.execute('SELECT current_tracklist_id FROM albums WHERE id=?',(published['id'],)).fetchone()[0]==published['current_tracklist_id']
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        (tmp_path/'release-occurrences-report.json').write_text(json.dumps(report,indent=2))
        print(report)
    finally:
        current.contract.cache_clear()
        store.conn.close()
