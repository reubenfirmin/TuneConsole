"""Opt-in listening cutover on a read-only, retained actual-library donor."""
import json
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core import collection_recovery, policy_documents
from yt_playlist.core.migrations import current
from yt_playlist.core.store import Store


def storage(conn):
    return conn.execute("SELECT coalesce(sum(pgsize),0) FROM dbstat WHERE name IN (SELECT name FROM sqlite_schema "
        "WHERE tbl_name IN ('play_events','listening_observations','trend_first_play','recording_listening_summary',"
        "'artist_listening_summary','listening_summary_state'))").fetchone()[0]


def test_actual_listening_cutover_preserves_evidence_precision_and_canonical_outputs(request,tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply a retained schema-99 actual-library donor')
    source = Path(option).resolve(strict=True)
    assert source.is_relative_to(Path(request.config.rootpath).resolve()/'.pytest_cache')
    target = tmp_path/'listening-subjects.db'
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as donor:
        assert donor.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]==99
        with policy_documents.retained_reader(collection_recovery.directory(donor)):
            donor.execute('BEGIN')
            with sqlite3.connect(target) as copied:
                donor.backup(copied)
            manifest = collection_recovery.export(donor,tmp_path/'collection-backup')
            donor.rollback()
    store = Store(target)
    try:
        collection_recovery.restore(store.conn,manifest)
        originals = {t:[dict(r) for r in store.conn.execute('SELECT * FROM '+t+' ORDER BY id')]
                     for t in ('play_events','listening_observations')}
        protected = ('player_events','play_event_sources','play_event_calendar','listening_capture_receipts',
            'recordings','catalog_entries','metadata_observations','metadata_conflicts','account_albums',
            'actions','predictions','prediction_inputs','recommendation_pools','album_track_occurrences','album_share_items')
        before = {t:store.conn.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in protected}
        sources = list(map(tuple,store.conn.execute('SELECT * FROM play_event_sources ORDER BY event_id,source')))
        calendars = list(map(tuple,store.conn.execute('SELECT * FROM play_event_calendar ORDER BY event_id')))
        days = list(map(tuple,store.conn.execute('SELECT * FROM listening_days ORDER BY identity_id,identity_key,day')))
        expected = list(map(tuple,store.conn.execute('''SELECT pe.identity_id,pe.recording_id,min(pe.played_at)
            FROM play_events pe WHERE pe.recording_id IS NOT NULL AND coalesce(pe.is_generated,0)=0
            AND EXISTS(SELECT 1 FROM play_event_sources s WHERE s.event_id=pe.id)
            GROUP BY pe.identity_id,pe.recording_id ORDER BY 1,2''')))
        artist_days = list(map(tuple,store.conn.execute('''WITH credits AS (
            SELECT DISTINCT e.recording_id,coalesce(r.artist_id,a.artist_id) artist_id FROM catalog_entries e
            JOIN entry_artists a ON a.entry_id=e.id LEFT JOIN artist_redirects r ON r.retired_id=a.artist_id)
            SELECT l.identity_id,c.artist_id,min(l.day) FROM listening_activity l JOIN credits c USING(recording_id)
            GROUP BY l.identity_id,c.artist_id ORDER BY 1,2''')))
        started = time.monotonic()
        sql_before = storage(store.conn)
        store.init_schema()
        elapsed = time.monotonic()-started
        for table,rows in originals.items():
            relation = 'play_event_subjects' if table=='play_events' else 'listening_observation_subjects'
            actual = list(store.conn.execute('SELECT * FROM '+relation+' ORDER BY id'))
            assert len(actual)==len(rows)
            for old,new in zip(rows,actual):
                assert {k:new[k] for k in old if k!='source' or table!='play_events'}=={
                    k:v for k,v in old.items() if k!='source' or table!='play_events'}
        assert {t:store.conn.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in protected}==before
        assert list(map(tuple,store.conn.execute('SELECT * FROM play_event_sources ORDER BY event_id,source')))==sources
        assert list(map(tuple,store.conn.execute('SELECT * FROM play_event_calendar ORDER BY event_id')))==calendars
        assert list(map(tuple,store.conn.execute('SELECT * FROM listening_days ORDER BY identity_id,identity_key,day')))==days
        started = time.monotonic()
        store.trends.refresh_first_play()
        rebuilt = time.monotonic()-started
        assert list(map(tuple,store.conn.execute('SELECT account_id,recording_id,first_exact_play_at '
            'FROM recording_listening_summary WHERE first_exact_play_at IS NOT NULL ORDER BY 1,2')))==expected
        actual_artists = list(map(tuple,store.conn.execute('''SELECT account_id,artist_id,min(day) FROM (
            SELECT account_id,artist_id,first_exact_day day FROM artist_listening_summary
            UNION ALL SELECT account_id,artist_id,first_observed_day FROM artist_listening_summary
            UNION ALL SELECT account_id,artist_id,first_legacy_unknown_day FROM artist_listening_summary)
            WHERE day IS NOT NULL GROUP BY account_id,artist_id ORDER BY 1,2''')))
        assert actual_artists==artist_days
        original_summaries = {t:list(map(tuple,store.conn.execute('SELECT * FROM '+t+' ORDER BY 1,2')))
                              for t in ('recording_listening_summary','artist_listening_summary')}
        store.trends.refresh_first_play()
        assert {t:list(map(tuple,store.conn.execute('SELECT * FROM '+t+' ORDER BY 1,2')))
                for t in original_summaries}==original_summaries
        with store.read_snapshot() as snapshot:
            prepared = snapshot.store.trends.prepare_first_play()
            assert prepared['artist_listening_summary']
            assert snapshot.publish(lambda:store.trends.publish_rollup(prepared,'{"story":null}',time.time(),None))
        current.validate(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        report = {'play_events':len(originals['play_events']),'observations':len(originals['listening_observations']),
            'unresolved_events':sum(r['recording_id'] is None for r in originals['play_events']),
            'unresolved_observations':sum(r['recording_id'] is None for r in originals['listening_observations']),
            'artist_account_rows':len(actual_artists),'recording_account_rows':len(original_summaries['recording_listening_summary']),
            'upgrade_seconds':elapsed,'summary_seconds':rebuilt,'protected':before,'sqlite':sqlite3.sqlite_version,
            'sql_bytes_before':sql_before,'sql_bytes_after':storage(store.conn),'new_payload_file_bytes':0}
        (tmp_path/'listening-subjects-report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,sort_keys=True))
        store.conn.close()
        store = Store(target)
        store.init_schema()
        assert store.trends.first_play_map('artist')
    finally:
        store.conn.close()
