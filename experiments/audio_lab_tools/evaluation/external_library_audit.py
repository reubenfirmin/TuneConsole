"""Audit frozen local prediction lineage before selecting an audio training pool."""
from collections import Counter, defaultdict
import json

from ..studies.external_checkpoint import RUN
from ..support.external_experiment import OUT, digest, save, stamp
from ..datasets.external_secondary import identity


def audit():
    base = RUN / 'inputs'
    manifest = json.loads((base / 'manifest.json').read_text())
    for filename in ('library.json', 'human-labels.json'):
        if digest(base / filename) != manifest['files'][filename]:
            raise ValueError('Frozen library audit input changed: ' + filename)
    library = json.loads((base / 'library.json').read_text())
    human = json.loads((base / 'human-labels.json').read_text())
    samples = {row['id']: row for row in library['sample_attributes']}
    embeddings = {row['recording_id'] for row in samples.values() if row['has_embedding']}
    measurements = {row['id']: row for row in library['measurements']}
    recordings = {row['id']: row for row in library['recordings']}
    contributors, missing = defaultdict(set), defaultdict(set)
    for link in library['prediction_inputs']:
        measurement = measurements.get(link['measurement_id'])
        if measurement is None:
            missing[link['prediction_id']].add(link['measurement_id'])
        else:
            contributors[link['prediction_id']].add(measurement['recording_id'])
    measured = defaultdict(set)
    for row in library['measurements']:
        measured[row['source']].add(row['recording_id'])
    gold_recordings, gold_artists, ambiguous = set(), set(), []
    for row in human['gold']:
        ids = {samples[key]['recording_id'] for key in row['sample_ids'] if key in samples}
        if len(ids) != 1:
            ambiguous.append({'video_id': row['video_id'], 'recording_ids': sorted(ids)})
            continue
        gold_recordings.update(ids)
        if name := identity(row['artist']):
            gold_artists.add(name)
    teacher_models = Counter(row['model_version'] for row in library['measurements']
                             if row['source'] == 'audio_measurement')
    by_kind = {}
    for kind in sorted({row['kind'] for row in library['predictions']}):
        rows = [row for row in library['predictions'] if row['kind'] == kind]
        by_kind[kind] = {
            'prediction_rows': len(rows), 'target_recordings': len({row['recording_id'] for row in rows}),
            'complete_lineage_rows': sum(bool(row['lineage_complete']) and not missing[row['id']] for row in rows),
            'unique_contributor_recording_sets': len({frozenset(contributors[row['id']]) for row in rows}),
            'rows_with_gold_contributors': sum(bool(contributors[row['id']] & gold_recordings) for row in rows),
            'rows_with_self_contribution': sum(row['recording_id'] in contributors[row['id']] for row in rows),
            'rows_referencing_missing_measurements': sum(bool(missing[row['id']]) for row in rows)}
    heldout_contamination = []
    for gold in human['gold']:
        target_ids = {samples[key]['recording_id'] for key in gold['sample_ids'] if key in samples}
        artist = identity(gold['artist'])
        heldout = target_ids | {key for key, row in recordings.items()
                               if artist and identity(row['artist']) == artist}
        relevant = [row for row in library['predictions'] if row['recording_id'] in target_ids]
        heldout_contamination.append({'video_id': gold['video_id'], 'prediction_rows': len(relevant),
            'rows_with_self_or_heldout_artist_contributor': sum(
                bool(contributors[row['id']] & heldout) for row in relevant),
            'rows_with_incomplete_lineage': sum(not row['lineage_complete'] or bool(missing[row['id']])
                                                for row in relevant)})
    candidate_ids = sorted(embeddings - gold_recordings)
    # Do not promote predictions to independent labels. This list identifies
    # recordings with actual captured embeddings for later source-only scoring.
    report = {'created_at': stamp(), 'input_manifest_sha256': digest(base / 'manifest.json'),
        'script_sha256': digest(__file__), 'current_vibe_recordings': len({row['recording_id'] for row in library['recording_vibes']}),
        'captured_embedding_recordings': len(embeddings), 'human_embedding_recordings': len(gold_recordings),
        'unlabeled_embedding_recordings': len(candidate_ids),
        'unlabeled_embedding_recording_ids': candidate_ids,
        'unlabeled_embedding_recordings_with_gold_artist': sum(
            identity(recordings[key]['artist']) in gold_artists for key in candidate_ids),
        'ambiguous_human_recording_joins': ambiguous,
        'measurement_recordings_by_source': {key: len(value) for key, value in measured.items()},
        'audio_teacher_model_versions': dict(teacher_models), 'propagation_by_kind': by_kind,
        'gold_prediction_lineage_checks': heldout_contamination,
        'new_labels_previously_in_historical_silver': human['new_labels_previously_in_silver'],
        'policy': ['Only actual compatible captured embeddings can enter audio representation learning or distillation.',
                   'Use source predictions as soft teacher outputs with model/version and direct/propagated provenance.',
                   'Collapse repeated captures and propagated descendants to their underlying recording evidence.',
                   'Recompute propagation inside each training fold after excluding held-out artists and contributors.',
                   'Exclude incomplete/missing lineage from the primary propagation experiment.',
                   'Score-only library tracks remain candidates for metadata propagation evaluation and listening selection.'],
        'limitations': ['A captured embedding flag does not yet establish compatibility with every external extractor.',
                       'Exact normalized artist strings do not resolve aliases or collaborations.',
                       'Current serving predictions are observational; their accuracy here is not held-out model evidence.',
                       'The frozen source transactions were separate; no atomic snapshot across both exports is claimed.']}
    save(OUT / 'checkpoint-1/library-audit.json', report)
    print(json.dumps({key: report[key] for key in ('current_vibe_recordings', 'captured_embedding_recordings',
          'human_embedding_recordings', 'unlabeled_embedding_recordings',
          'unlabeled_embedding_recordings_with_gold_artist', 'propagation_by_kind')}), flush=True)
    return report


if __name__ == '__main__':
    audit()
