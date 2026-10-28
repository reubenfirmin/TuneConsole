"""Evidence and blind listening remain separate, scoped, versioned and retractable."""
from copy import deepcopy

import pytest

from experiments.audio_lab_tools.evaluation import observations, packet, validate


def _report(char='a', *, synthetic=False, artist='Artist', album='Album'):
    source = 'sha256:' + char * 64
    return {'schema': 1, 'recommendation_adoption': False, 'human_judgments': None,
        'reference': {'source': source, 'title': 'Track', 'artist': artist, 'album': album,
                      'filename': 'reference.wav', 'permission_note': 'Original recording', 'synthetic': synthetic},
        'results': [{'schema': 1, 'id': 'session/0', 'source': source, 'start': 2, 'end': 12,
                     'duration': 100, 'sample_rate': 16000, 'sample_count': 160000,
                     'policy': 'reference-windows-1', 'derivation_version': 'signal-descriptors-1',
                     'raw_audio_retained': False, 'recording_association': None, 'intensity': None,
                     'descriptors': {'rms': .1, 'rms_dbfs': -20},
                     'prediction': {'kind': 'prediction', 'model': 'msd-musicnn-1', 'model_version': '1',
                         'feature_policy': 'musicnn-512-256-full-patches-1', 'bundle': {'files': []},
                         'reliability': None, 'scores': {'instrumental': .8},
                         'analysed_seconds': 8.99, 'inference_ms': 30}}]}


def test_blind_packet_excludes_synthetic_predictions_and_preserves_holdouts():
    reports = [_report('a'), _report('b'), _report('c', synthetic=True)]
    listener, evidence, report = packet(reports)
    assert len(listener['tracks']) == 12
    assert all(row['fit'] is None and row['intrusion'] is None for row in listener['tracks'])
    assert all(row['more_intense'] is None for row in listener['intensity_pairs'])
    assert not any(set(row) & {'scores', 'split', 'selection', 'affinity', 'prediction'} for row in listener['tracks'])
    assert len({row['group'] for row in evidence['manifest']}) == 1
    assert len({row['split'] for row in evidence['manifest']}) == 1
    assert report['reference_music_files'] == 2
    assert report['synthetic_files_excluded_from_listening'] == 1
    assert report['human_judgments'] == 0
    assert report['production_decision'] == 'no_go_pending_evidence'
    with pytest.raises(ValueError, match='one export'):
        packet([reports[0], reports[0]])


@pytest.mark.parametrize('field,value', [('source', 'wrong'), ('intensity', .9),
    ('recording_association', 'guessed'), ('end', 50), ('sample_count', 50),
    ('raw_audio_retained', True), ('policy', ''), ('duration', float('nan'))])
def test_ambiguous_or_unversioned_evidence_is_rejected(field, value):
    report = _report()
    report['results'][0][field] = value
    with pytest.raises(ValueError):
        validate(report)


def test_observations_keep_native_units_actual_model_coverage_and_version_identity(store):
    report = _report()
    before = deepcopy(report)
    evidence = observations(report)
    assert report == before
    assert [row.field for row in evidence] == ['audio.rms', 'audio.rms_dbfs', 'audio.native_tag.instrumental']
    assert [row.kind for row in evidence] == ['measurement', 'measurement', 'prediction']
    assert all(row.scope == 'item' and row.entity_id == report['reference']['source'] for row in evidence)
    assert evidence[1].unit == 'dBFS'
    assert evidence[2].match_evidence['interval_s'] == [2, 10.99]
    assert evidence[2].match_evidence['sample_coverage'] == pytest.approx(.0899)
    assert evidence[2].match_evidence['reliability'] is None
    # Scratch-only ingestion uses existing idempotence and leaves selected corrections intact.
    tid = store.tracks.upsert_track('video', 'Song', 'Artist', '', 100)
    store.enrichment.set_track_field(tid, 'energy', .2)
    selected = store.conn.execute('SELECT energy FROM catalog_tracks WHERE id=?', (tid,)).fetchone()[0]
    for _ in range(2):
        store.enrichment_state.record_metadata_observations(tid, 'audio-lab', evidence, run_id='same', now=10)
    assert store.conn.execute('SELECT COUNT(*) FROM metadata_findings WHERE provider=?', ('audio-lab',)).fetchone()[0] == 3
    assert store.conn.execute('SELECT energy FROM catalog_tracks WHERE id=?', (tid,)).fetchone()[0] == selected
    report['results'][0]['prediction']['model_version'] = '2'
    assert observations(report)[2].payload() != evidence[2].payload()
    report['results'][0]['prediction']['model_version'] = '1'
    report['results'][0]['prediction']['runtime_version'] = 'essentia-csp-musicnn-1'
    assert observations(report)[2].payload() != evidence[2].payload()
