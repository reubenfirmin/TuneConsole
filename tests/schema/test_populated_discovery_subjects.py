"""Opt-in cutover on a read-only retained actual-library predecessor."""
import json
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core import collection_recovery, discovery_provenance as provenance, policy_documents
from yt_playlist.core.migrations import current
from yt_playlist.core.store import Store


def storage(conn):
    return conn.execute("SELECT coalesce(sum(pgsize),0) FROM dbstat WHERE name IN (SELECT name FROM sqlite_schema "
        "WHERE tbl_name IN ('artist_scans','discovered_artists','discovery_sources','discovery_artist_sources',"
        "'discovery_visible_impressions','discovery_origins','discovery_impressions',"
        "'unresolved_discovery_impressions','discovery_legacy_offers'))").fetchone()[0]


def test_actual_discovery_cutover_preserves_sources_unknowns_views_and_restart(request,tmp_path):
    option = request.config.getoption('--schema-recovery-db')
    if not option:
        pytest.skip('Supply a retained schema-100 actual-library donor')
    source = Path(option).resolve(strict=True)
    assert source.is_relative_to(Path(request.config.rootpath).resolve()/'.pytest_cache')
    target = tmp_path/'discovery-subjects.db'
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as donor:
        assert donor.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]==100
        with policy_documents.retained_reader(collection_recovery.directory(donor)):
            donor.execute('BEGIN')
            with sqlite3.connect(target) as copied:
                donor.backup(copied)
            manifest = collection_recovery.export(donor,tmp_path/'collection-backup')
            donor.rollback()
    store = Store(target)
    try:
        collection_recovery.restore(store.conn,manifest)
        original = {t:[dict(r) for r in store.conn.execute('SELECT * FROM '+t+' ORDER BY rowid')]
            for t in ('artist_scans','discovered_artists','discovery_sources','discovery_artist_sources','discovery_visible_impressions')}
        sources = sorted((r['recording_id'],r['source_token'],r['input_video_id'],r['found_at'],r['last_found_at'])
            for r in original['discovery_sources'])
        protected = ('recordings','catalog_entries','youtube_items','artists','albums','player_events','play_events',
            'play_event_sources','listening_observations','metadata_observations','metadata_conflicts','account_albums',
            'actions','predictions','prediction_inputs','recommendation_pools','album_track_occurrences','album_share_items')
        before = {t:store.conn.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in protected}
        expected_views,unresolved = set(),set()
        for row in original['discovery_visible_impressions']:
            value = int(row['item_id']) if row['kind']=='artist' and row['item_id'].isdigit() else row['item_id']
            identifier = provenance.subject(store.conn,row['kind'],value)
            if identifier is None:
                unresolved.add((row['view_id'],row['kind'],row['item_id'],row['shown_at']))
            else:
                expected_views.add((row['view_id'],row['kind'],identifier,row['shown_at']))
        sql_before = storage(store.conn)
        started = time.monotonic()
        store.init_schema()
        elapsed = time.monotonic()-started
        converted = sorted((r['recording_id'],r['source_token'],r['input_video_id'],r['first_found_at'],r['last_found_at'])
            for r in store.conn.execute('SELECT s.*,o.source_token FROM discovery_sources s JOIN discovery_origin_tokens o ON o.id=s.origin_id'))
        assert converted==sources
        assert store.conn.execute('SELECT count(*) FROM discovery_artist_sources').fetchone()[0]==len(original['discovery_artist_sources'])
        typed_views = set()
        for row in store.conn.execute('SELECT * FROM discovery_impressions'):
            for kind,column in provenance.SUBJECTS.items():
                if row[column] is not None:
                    typed_views.add((row['view_id'],kind,row[column],row['shown_at']))
        assert typed_views==expected_views
        assert set(map(tuple,store.conn.execute('SELECT view_id,original_kind,original_key,shown_at FROM unresolved_discovery_impressions')))==unresolved
        assert {t:store.conn.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in protected}==before
        candidates = {r['artist_id']:r for r in store.discovery.get_discovered_artists()}
        for row in original['discovered_artists']:
            assert candidates[row['artist_id']]['because']==json.loads(row['because'] or '[]')
            assert candidates[row['artist_id']]['fits']==json.loads(row['fits'] or '[]')
        unknown = store.conn.execute("SELECT count(*) FROM discovery_artist_sources s JOIN discovery_origins o "
            "ON o.id=s.origin_id WHERE o.kind='legacy_unknown'").fetchone()[0]
        assert unknown==sum(r['source']=='unknown' for r in original['discovery_artist_sources'])
        current.validate(store.conn)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        report = {'sqlite':sqlite3.sqlite_version,'upgrade_seconds':elapsed,'protected':before,
            'scans':len(original['artist_scans']),'artist_candidates':len(candidates),
            'recording_sources':len(sources),'artist_sources':len(original['discovery_artist_sources']),
            'retained_unknown_artist_sources':unknown,'typed_impressions':len(typed_views),
            'unresolved_impressions':len(unresolved),'sql_bytes_before':sql_before,'sql_bytes_after':storage(store.conn),
            'candidate_output_bytes':sum(r[0] for r in store.conn.execute('SELECT recovery_bytes FROM discovered_artists WHERE recovery_bytes IS NOT NULL'))}
        (tmp_path/'discovery-subjects-report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,sort_keys=True))
        # Continuing discovery on this isolated copy reuses retained lifetime
        # facts; no special pre-prepared candidate state is required.
        selected = next(iter(candidates.values()))
        aid = selected['artist_id']
        prior = selected['offered_count']
        store.discovery.record_discovery_impressions('artist',[aid],'schema499-actual-visit',time.time())
        store.discovery.record_discovery_impressions('artist',[aid],'schema499-actual-visit',time.time())
        assert store.conn.execute('SELECT offered_count FROM discovered_artists WHERE artist_id=?',(aid,)).fetchone()[0]==prior+1
        store.conn.close()
        store = Store(target)
        store.init_schema()
        assert store.conn.execute('SELECT offered_count FROM discovered_artists WHERE artist_id=?',(aid,)).fetchone()[0]==prior+1
    finally:
        store.conn.close()
