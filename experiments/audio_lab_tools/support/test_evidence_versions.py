"""Read-only reports retain exact version text in both admitted layouts."""
from contextlib import closing
import hashlib
import json
import shutil
import sqlite3

from experiments.audio_lab_tools.support import evidence_audit
from experiments.audio_lab_tools.training import emotify_input_comparison as comparison
from tests.rec.test_vibe_prediction import capture, drain, fits, qualified_candidate, track
from tests.repos.test_recording_vibes import installed_v107_template, versioned_store
from yt_playlist.core import profile_files, vibe_versions
from yt_playlist.rec import audio_vibe_model, vibe_preparation


def test_readonly_reports_resolve_versions_and_preserve_saved_state(
        versioned_store, tmp_path, monkeypatch, qualified_candidate):
    store = versioned_store
    # A fixed artifact identity exercises version matching. The comparison's
    # model inference is controlled below; this does not qualify a music model.
    model = tmp_path / 'controlled-model.json'
    model.write_text(json.dumps({'labels': list(comparison.VIBES)}))
    model_hash = hashlib.sha256(model.read_bytes()).hexdigest()
    monkeypatch.setattr(audio_vibe_model, 'DEFAULT_PATH', model)
    monkeypatch.setattr(audio_vibe_model, 'identity', lambda: model_hash)
    _entry, original = track(store, 'original')
    track(store, 'target')
    samples = capture(store, 'original')
    drain(store)
    expected_version = vibe_preparation.version()
    expected_scores = {vibe: fit['score'] for vibe, fit in fits(store, original).items()}
    database = tmp_path / 'readonly.db'
    with closing(sqlite3.connect(database)) as copy:
        store.conn.backup(copy)
        shutil.copytree(profile_files.directory(store.conn), profile_files.directory(copy))
    before = database.read_bytes()

    audit = tmp_path / 'audit'
    evidence_audit.snapshot(database, audit)
    report = json.loads((audit / 'snapshot.json').read_text())
    measured = report['cases']['vibe:audio_measurement']['current_vibes']
    expected_channels = set(comparison.VIBES) | {'shared_release', 'shared_energize'}
    assert {row['vibe'] for row in measured} == expected_channels
    assert len(measured) == 8  # Six trained targets plus two shared intensity channels.
    assert {row['inputs_version'] for row in measured} == {expected_version}
    assert {row['model_version'] for row in measured} == {expected_version}
    assert all('inputs_version_id' not in row for row in measured)
    assert {sample_id for row in measured for sample_id in row['sample_ids']} == {
        sample['id'] for sample in samples}
    predicted = report['cases']['vibe:artist_genre']['current_vibes']
    assert {row['vibe'] for row in predicted} == expected_channels
    assert all(isinstance(row['policy_version'], str) and model_hash in row['policy_version']
               for row in predicted)

    output = tmp_path / 'comparison'
    output.mkdir()
    monkeypatch.setattr(comparison, 'OUT', output)
    frozen = tmp_path / 'gold.json'
    frozen.write_text(json.dumps({'gold': [{
        'video_id': 'original', 'labels': ['relax'],
        'baseline': {vibe: .5 for vibe in comparison.VIBES},
        'values': [None] * len(audio_vibe_model.FEATURES),
    }]}))
    monkeypatch.setattr(comparison, 'SNAPSHOT', frozen)
    (output / 'emotify-input-comparison.report.json').write_text(json.dumps({'limitations': []}))
    (output / 'emotify-input-comparison.predictions.json').write_text(json.dumps({
        'video_ids': ['original'], 'predictions': {
            name: [[.5] * len(comparison.VIBES)] for name in (
                'transfer:provider_shared', 'direct_gold:provider_shared')},
    }))
    # Saved trial ranking is outside this schema check; exercise the actual
    # read-only library queries and complete-capture score/version comparison.
    monkeypatch.setattr(comparison, 'add_ranking_metrics', lambda *args, **kwargs: {})
    inferred = []

    def scores(_model, values):
        assert {row['id'] for row in values} == {row['id'] for row in samples}
        inferred.append(True)
        return expected_scores

    monkeypatch.setattr(comparison.audio_vibe_external, 'scores', scores)
    comparison.summarize(database)
    actual = json.loads((output / 'emotify-input-comparison.report.json').read_text())['actual_library']
    assert actual['library_recordings_with_video'] == 2
    assert actual['current_model_version_source_counts']['audio_measurement'] == 1
    assert actual['complete_capture_recordings'] == 1
    assert actual['compatible_capture_recordings'] == 1
    assert actual['prepared_current_model_matches'] == 1
    assert actual['prepared_stale_or_missing'] == 0
    assert actual['maximum_current_model_score_error'] == 0
    assert inferred == [True]
    assert database.read_bytes() == before

    # One stale required target must prevent a match, even when the other
    # current target versions still count this recording in the source totals.
    with store.conn:
        column = vibe_versions.column(store.conn, 'recording_vibes')
        store.conn.execute(f'UPDATE recording_vibes SET {column}=? WHERE vibe=? AND '
                           'recording_id=(SELECT id FROM recordings WHERE identity_key=?)',
                           (vibe_versions.stored(store.conn, 'outdated'), 'relax', original))
    stale = tmp_path / 'stale.db'
    with closing(sqlite3.connect(stale)) as copy:
        store.conn.backup(copy)
    stale_before = stale.read_bytes()
    comparison.summarize(stale)
    actual = json.loads((output / 'emotify-input-comparison.report.json').read_text())['actual_library']
    assert actual['current_model_version_source_counts']['audio_measurement'] == 1
    assert actual['prepared_current_model_matches'] == 0
    assert actual['prepared_stale_or_missing'] == 1
    assert inferred == [True, True]
    assert stale.read_bytes() == stale_before
