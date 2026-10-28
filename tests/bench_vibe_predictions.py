"""Explicit saved-library acceptance, using a small isolated catalog projection.

Run explicitly under pytest's machine lock. The source is opened mode=ro and
query_only; schema initialization and all preparation run only in tmp_path.
The source's large recommendation history is unnecessary for this workflow.
"""
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from yt_playlist.core.store import Store
from yt_playlist.core.migrations import migrate
from yt_playlist.core.migrations.registry import MIGRATIONS
from yt_playlist.rec import audio_vibe_model, audio_vibe_preview, musical_character, musical_preparation
from yt_playlist.rec import vibe_preparation, musical_profiles
from yt_playlist.rec.progressive import Preparation
from yt_playlist.util.matching import normalize


def test_saved_library_vibe_startup(tmp_path, monkeypatch, record_property, request):
    path = Path(request.config.getoption('vibe_readonly_db') or os.environ.get(
        'TUNECONSOLE_VIBE_SOURCE', '/home/rfirmin/.local/share/yt-playlist/state.db'))
    if not path.exists():
        pytest.skip('Explicit local saved-library acceptance requires the saved database')
    artifact = Path('experiments/audio_lab/local/vibe-subgenre-model.json').resolve()
    if artifact.exists():
        monkeypatch.setenv(audio_vibe_model.ENVIRONMENT_KEY, str(artifact))
    source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    source.execute('PRAGMA query_only=ON')
    source.execute('BEGIN')
    source_version = source.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
    store = Store(tmp_path / 'vibe-catalog.db')
    if source_version < 61:
        migrate(store.conn, registry=MIGRATIONS[:60])
    else:
        store.init_schema()
    tables = ('artists', 'recordings', 'youtube_items', 'catalog_entries', 'unresolved_items',
              'track_artists', 'entry_field_choices', 'library_memberships', 'discovery_memberships',
              'discovery_sources', 'rec_musical_sources', 'rec_musical_links',
              'audio_vibe_samples' if source_version < 61 else 'sample_attributes')
    counts = {}
    started = time.monotonic()
    try:
        # Copy exact retained rows, not synthetic approximations or prepared fits.
        with store.conn:
            for table in tables:
                columns = [row[1] for row in source.execute(f'PRAGMA table_info({table})')]
                query = source.execute(f'SELECT * FROM {table}')
                sql = f'INSERT INTO {table} (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
                counts[table] = 0
                while rows := query.fetchmany(256):
                    store.conn.executemany(sql, rows)
                    counts[table] += len(rows)
        store.init_schema()  # Upgrade only the disposable projection, including sample files.
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        # Feed captured songs first, as the upgrade migration does. Background
        # work still handles an initially empty provider/profile/prediction cache.
        with store.conn:
            store.conn.execute('DELETE FROM rec_content_pending')
            store.conn.execute('INSERT INTO rec_content_pending(identity_key) SELECT DISTINCT r.identity_key '
                'FROM sample_attributes s JOIN recordings r ON r.id=s.recording_id')
        worker = Preparation(store)
        first_audio_at = None
        cold_started = time.monotonic()
        for cold_steps in range(12):
            worker.capture(1000)
            if first_audio_at is None and store.conn.execute(
                    "SELECT 1 FROM recording_vibes WHERE source='audio_measurement' LIMIT 1").fetchone():
                first_audio_at = time.monotonic() - cold_started
        assert first_audio_at is not None
        cold = {'first_audio_seconds': first_audio_at,
                'profiles': store.conn.execute('SELECT COUNT(*) FROM rec_musical_profiles').fetchone()[0],
                'pending_provider_sources': store.conn.execute('SELECT COUNT(*) FROM rec_musical_pending').fetchone()[0],
                'seconds': time.monotonic() - cold_started}
        # Upgrade rehearsal: retain the actual preexisting provider caches,
        # while rebuilding every vibe cache from zero. The separate phase above
        # establishes useful cold output without waiting for all provider work.
        with store.conn:
            store.conn.executemany('INSERT OR REPLACE INTO rec_musical_source_profiles VALUES (?,?,?,?,?)',
                                  source.execute('SELECT * FROM rec_musical_source_profiles'))
            actual_pending = {r[0] for r in source.execute('SELECT source_key FROM rec_musical_pending')}
            cached = [r[0] for r in store.conn.execute(
                'SELECT source_key FROM rec_musical_source_profiles WHERE version=?', (musical_profiles.VERSION,))
                      if r[0] not in actual_pending]
            store.conn.executemany('DELETE FROM rec_musical_pending WHERE source_key=?', [(key,) for key in cached])
            for table in ('recording_vibes', 'prediction_inputs', 'prediction_artists', 'predictions',
                          'measurement_samples', 'measurements', 'rec_vibe_pending',
                          'rec_musical_profiles', 'rec_content_pending'):
                store.conn.execute(f'DELETE FROM {table}')
            store.conn.execute("INSERT INTO rec_vibe_pending(group_key) VALUES ('*')")
        source.close()
        # Match app restart: bulk fixture import must not manufacture a session
        # containing hundreds of thousands of unrelated TEMP revision entries.
        store.conn.close()
        store = Store(tmp_path / 'vibe-catalog.db')
        store.init_schema()
        worker = Preparation(store)
        rollout_started = time.monotonic()
        eligible = {r[0] for r in store.conn.execute(
            'SELECT r.identity_key FROM recordings r WHERE EXISTS '
            '(SELECT 1 FROM catalog_entries e JOIN library_memberships m ON m.entry_id=e.id WHERE e.recording_id=r.id) '
            'OR EXISTS (SELECT 1 FROM discovery_memberships d WHERE d.recording_id=r.id)')}
        for steps in range(15000):
            # Measure initial corpus coverage during continuing provider work,
            # rather than requiring a continuously changing system to be idle.
            if steps % 16 == 0 and eligible <= {r[0] for r in store.conn.execute(
                    'SELECT DISTINCT r.identity_key FROM recording_vibes v JOIN recordings r ON r.id=v.recording_id')}:
                break
            assert time.monotonic() - rollout_started < 300, 'Saved-library vibe upgrade exceeded five minutes'
            worker.capture(1000)
        else:
            pytest.fail('Saved-library queue did not converge')
        profiles = store.conn.execute('SELECT identity_key,payload FROM rec_musical_profiles').fetchall()
        keys = [row[0] for row in profiles]
        profiles = [(key, json.dumps(profile)) for start in range(0, len(keys), 512)
                    for key, profile in store.musical_profiles.by_keys(keys[start:start + 512]).items()]
        layers = Counter()
        measured = 0
        radio = {}
        for key, payload in profiles:
            profile = json.loads(payload)
            layers.update(row['source'] for row in profile['vibes']['fits'].values())
            if profile['vibes']['fits']['relax']['source'] == 'audio_measurement':
                inputs = store.vibe_predictions.inputs([key])[key]
                expected = audio_vibe_preview.predict(inputs['samples'], metadata=inputs['metadata'])
                actual = profile['vibes']['fits']
                assert {row['vibe']: row['score'] for row in expected['ranking']} == {
                    vibe: row['score'] for vibe, row in actual.items()}
                measured += 1
            for title in ('Resonance', 'Oval Moon (IBC mx)', 'Mininga'):
                if key.startswith(normalize(title) + '|'):
                    radio[title] = {'intensity': musical_character.intensity({'musical': profile}),
                                    'fit': profile['vibes']['fits']['energize']}
        assert measured > 0 and layers['provider_measurement'] > 0
        assert sum(layers[k] for k in ('artist_genre', 'artist', 'genre')) > 0
        assert eligible <= {row[0] for row in profiles}
        assert not store.conn.execute('SELECT 1 FROM audio_session_labels').fetchone()
        report = {'source_schema': source_version, 'runtime': vibe_preparation.version(), 'copied_rows': counts,
                  'profiles': len(profiles), 'measured_recordings': measured, 'layers': dict(layers),
                  'steps': steps, 'cold_start': cold, 'upgrade_seconds': time.monotonic() - rollout_started,
                  'total_seconds': time.monotonic() - started, 'radio_examples': radio}
        report['continuing_provider_sources'] = store.conn.execute('SELECT COUNT(*) FROM rec_musical_pending').fetchone()[0]
        report['continuing_vibe_jobs'] = store.conn.execute('SELECT COUNT(*) FROM rec_vibe_pending').fetchone()[0]
        report['eligible_recordings'] = len(eligible)
        report_path = Path('/tmp/tuneconsole-464-saved-library-report.json')
        report_path.write_text(json.dumps(report, indent=2))
        record_property('saved_library_vibe_report', json.dumps(report))
        print(json.dumps(report, indent=2))
    finally:
        source.close()
        store.conn.close()
