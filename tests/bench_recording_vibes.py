"""Explicit populated-upgrade acceptance; source remains pinned and read-only."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core import sample_files
from yt_playlist.core.store import Store


def test_genuine_recording_vibe_upgrade(tmp_path, request, record_property):
    source_path = request.config.getoption('vibe_readonly_db')
    if not source_path:
        pytest.skip('Pass --vibe-readonly-db with a genuine schema-60 saved-library snapshot')
    source_path = Path(source_path).resolve()
    with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source:
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        assert source.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 60
        samples = list(source.execute('SELECT id,payload,created_at FROM audio_vibe_samples ORDER BY id'))
        labels = {table: list(source.execute(f'SELECT * FROM {table} ORDER BY run_id')) for table in (
            'audio_vibe_feedback', 'audio_vibe_labels', 'audio_session_labels', 'audio_vibe_runs')}
        measured = [(key, vibe, fit['score'], fit['support'], version)
                    for key, raw, version in source.execute('SELECT identity_key,evidence,version FROM rec_vibe_tracks WHERE evidence IS NOT NULL')
                    for vibe, fit in json.loads(raw)['fits'].items()]
        served = {(key, vibe): (fit['score'], fit['support'], fit['source'])
                  for key, raw in source.execute('SELECT identity_key,payload FROM rec_musical_profiles')
                  for vibe, fit in json.loads(raw).get('vibes', {}).get('fits', {}).items()}
        destination = tmp_path / 'upgrade.db'
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
        stamp = hashlib.sha256(json.dumps(samples, sort_keys=True).encode()).hexdigest()
        started = time.monotonic()
        store = Store(destination)
        try:
            store.init_schema()
            upgrade_seconds = time.monotonic() - started
            assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
            for sid, raw, captured in samples:
                row = store.conn.execute('SELECT filename,captured_at FROM sample_attributes WHERE id=?', (sid,)).fetchone()
                assert sample_files.read(row[0]) == json.loads(raw)
                assert row[1] == captured
            for table, rows in labels.items():
                assert rows == list(map(tuple, store.conn.execute(f'SELECT * FROM {table} ORDER BY run_id')))
            current = {(key, vibe): (score, support, origin) for key, vibe, score, support, origin in store.conn.execute(
                'SELECT r.identity_key,v.vibe,v.score,v.support,v.source FROM recording_vibes v JOIN recordings r ON r.id=v.recording_id')}
            assert current == served
            retained = set(map(tuple, store.conn.execute('SELECT r.identity_key,m.vibe,m.score,m.support,m.model_version '
                'FROM measurements m JOIN recordings r ON r.id=m.recording_id')))
            assert set(measured) <= retained
            assert not store.conn.execute('SELECT 1 FROM predictions p WHERE p.lineage_complete<>0').fetchone()
            assert not store.conn.execute('SELECT 1 FROM prediction_inputs').fetchone()
            files = store.conn.execute('SELECT COUNT(*),COUNT(DISTINCT filename) FROM sample_attributes').fetchone()
            report = {'source': str(source_path), 'source_schema': 60, 'source_sample_sha256': stamp,
                      'recordings': store.conn.execute('SELECT COUNT(*) FROM recordings').fetchone()[0],
                      'samples': files[0], 'unique_files': files[1], 'retained_measurements': len(retained),
                      'retained_serving_scores': len(current), 'upgrade_seconds': upgrade_seconds,
                      'sqlite': sqlite3.sqlite_version}
            before = store.conn.total_changes
            store.init_schema()
            # Reopening must validate without changing canonical scores/files.
            assert current == {(key, vibe): (score, support, origin) for key, vibe, score, support, origin in store.conn.execute(
                'SELECT r.identity_key,v.vibe,v.score,v.support,v.source FROM recording_vibes v JOIN recordings r ON r.id=v.recording_id')}
            report['reopen_connection_changes'] = store.conn.total_changes - before
            Path('/tmp/tuneconsole-481-upgrade-report.json').write_text(json.dumps(report, indent=2))
            record_property('recording_vibe_upgrade_report', json.dumps(report))
            print(json.dumps(report, indent=2))
        finally:
            store.conn.close()
        assert stamp == hashlib.sha256(json.dumps(list(source.execute(
            'SELECT id,payload,created_at FROM audio_vibe_samples ORDER BY id')), sort_keys=True).encode()).hexdigest()
