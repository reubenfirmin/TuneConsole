"""Opt-in facade retirement and mutation measurements on our isolated library."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core import metadata
from yt_playlist.core.migrations import current
from yt_playlist.core.migrations.v102_catalog_writes import RETIRED
from yt_playlist.core.store import Store


def signatures(conn,tables,*,column_orders=None):
    result = {}
    for table in tables:
        digest = hashlib.sha256()
        count = 0
        keys = sorted((r[5],r[1]) for r in conn.execute('PRAGMA table_info('+table+')') if r[5])
        order = ','.join('"'+name.replace('"','""')+'"' for _,name in keys) or 'rowid'
        projection = ','.join('"'+name.replace('"','""')+'"' for name in column_orders[table]) if column_orders else '*'
        for row in conn.execute('SELECT '+projection+' FROM '+table+' ORDER BY '+order):
            digest.update(json.dumps(tuple(row),separators=(',',':'),ensure_ascii=False,
                default=lambda value: {'blob':value.hex()} if isinstance(value,bytes) else str(value)).encode())
            digest.update(b'\n')
            count += 1
        result[table] = {'rows':count,'sha256':digest.hexdigest()}
    return result


def test_populated_facade_retirement_and_explicit_write_measurements(request,tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply our retained isolated discovery-subjects.db')
    target = Path(option).resolve(strict=True)
    assert target.is_relative_to(Path(request.config.rootpath).resolve()/'.pytest_cache')
    assert target.name=='discovery-subjects.db'
    store = Store(target)
    try:
        version = store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
        assert version in (101,102)
        tables = ('recordings','youtube_items','catalog_entries','legacy_track_refs','library_memberships',
            'discovery_memberships','playlist_items','unresolved_playlist_items','playlist_membership_refs',
            'discovery_sources','discovery_origins','discovery_impressions','unresolved_discovery_impressions',
            'recording_metadata_selections','entry_metadata_selections','metadata_choice_heads',
            'metadata_observations','metadata_conflicts','play_events','play_event_sources','listening_observations',
            'album_track_occurrences','album_share_items','actions','predictions','prediction_inputs','recommendation_pools')
        before = signatures(store.conn,tables)
        projection = [tuple(r) for r in store.conn.execute('SELECT * FROM catalog_tracks ORDER BY id')]
        playlist = [tuple(r) for r in store.conn.execute('SELECT * FROM playlist_tracks ORDER BY playlist_id,position')]
        trigger_bytes_before = store.conn.execute("SELECT coalesce(sum(length(sql)),0) FROM sqlite_schema WHERE type='trigger'").fetchone()[0]
        started = time.monotonic()
        store.init_schema()
        upgrade_seconds = time.monotonic()-started
        assert signatures(store.conn,tables)==before
        assert [tuple(r) for r in store.conn.execute('SELECT * FROM catalog_tracks ORDER BY id')]==projection
        assert [tuple(r) for r in store.conn.execute('SELECT * FROM playlist_tracks ORDER BY playlist_id,position')]==playlist
        assert not RETIRED & {r[0] for r in store.conn.execute("SELECT name FROM sqlite_schema WHERE type='trigger'")}
        current.validate(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        entries = [r[0] for r in store.conn.execute('SELECT id FROM catalog_entries ORDER BY id LIMIT 100')]
        started = time.monotonic()
        for entry in entries:
            assert store.conn.execute('SELECT * FROM catalog_tracks WHERE id=?',(entry,)).fetchone()
        reads_seconds = time.monotonic()-started
        entry = store.conn.execute("SELECT e.id FROM catalog_entries e WHERE NOT EXISTS "
            "(SELECT 1 FROM metadata_choice_heads h WHERE h.entry_id=e.id AND h.field='bpm') ORDER BY e.id LIMIT 1").fetchone()[0]
        serial_before = store.prepared_content.serial()
        pending_before = signatures(store.conn,('rec_content_pending',))
        samples = []
        try:
            store.conn.execute('BEGIN IMMEDIATE')
            for index in range(5):
                started = time.monotonic()
                metadata.select(store.conn,entry,'bpm',121+index,'schema500-author-check',now=200+index)
                samples.append(time.monotonic()-started)
            assert store.conn.execute('SELECT bpm FROM catalog_tracks WHERE id=?',(entry,)).fetchone()[0]==125
            assert signatures(store.conn,('rec_content_pending',))!=pending_before
            # A mutation queues new preparation; it cannot claim that an old
            # prepared generation already contains the newly selected value.
            assert store.prepared_content.serial()==serial_before
        finally:
            store.conn.rollback()
        assert store.prepared_content.serial()==serial_before
        assert signatures(store.conn,('rec_content_pending',))==pending_before
        assert signatures(store.conn,('recordings','metadata_observations','recording_metadata_selections'))=={
            table:before[table] for table in ('recordings','metadata_observations','recording_metadata_selections')}
        report = {'sqlite':sqlite3.sqlite_version,'previous_version':version,'current_version':102,
            'upgrade_seconds':upgrade_seconds,'protected':before,'catalog_projection_rows':len(projection),
            'playlist_projection_rows':len(playlist),'point_reads':len(entries),'point_read_seconds':reads_seconds,
            'selected_write_seconds':samples,'retired_triggers':sorted(RETIRED),
            'trigger_sql_bytes_before':trigger_bytes_before,
            'trigger_sql_bytes_after':store.conn.execute("SELECT coalesce(sum(length(sql)),0) FROM sqlite_schema WHERE type='trigger'").fetchone()[0]}
        (tmp_path/'catalog-writes-report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k!='protected'},sort_keys=True))
    finally:
        store.conn.close()
