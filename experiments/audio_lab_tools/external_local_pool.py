"""Recover actual local acoustic inputs and frozen soft teachers, without DB writes."""
from collections import defaultdict
import json
from pathlib import Path

from yt_playlist.core import audio_embedding

from .external_checkpoint import RUN
from .external_experiment import OUT, ROOT, digest, save, stamp
from .external_library_audit import audit
from .external_secondary import identity
from .external_transfer import SNAPSHOT, VIBES


def teacher_training_provenance(measurement_rows):
    """Resolve known local-fit dependencies separately from measurement lineage."""
    product_path = ROOT / 'src/yt_playlist/rec/data/emotify-vibes.json'
    product = json.loads(product_path.read_text())
    historical = json.loads(SNAPSHOT.read_text())
    if product['provenance']['snapshot_sha256'] != digest(SNAPSHOT):
        raise ValueError('Product teacher training cohort does not match the preserved snapshot')
    current_hash = digest(product_path)
    old_teacher = historical['teacher']
    local_ids, native_sources, unresolved = set(), set(), set()
    for row in measurement_rows:
        if row['source'] == 'provider_measurement':
            # This path uses recording-level provider attributes and fixed
            # heuristics, even though its version stamp includes the audio model.
            continue
        if row['source'] != 'audio_measurement':
            unresolved.add(row['model_version'])
            continue
        version = row['model_version'].rsplit('/', 1)[-1]
        if version == current_hash:
            native_sources.add('emotify')
            local_ids.update(row['video_id'] for row in historical['gold']
                             if len(row.get('embedding_values', [])) == 200)
        elif version == old_teacher['model_sha256']:
            local_ids.update(old_teacher['training_video_ids'])
            native_sources.add('deam')
        elif version == 'heuristic':
            native_sources.add('deam')
        else:
            unresolved.add(row['model_version'])
    return {'local_training_video_ids': sorted(local_ids), 'native_training_corpora': sorted(native_sources),
        'unresolved_model_versions': sorted(unresolved), 'known_fit_dependencies_complete': not unresolved,
        'product_teacher_sha256': current_hash, 'historical_snapshot_sha256': digest(SNAPSHOT),
        'limitations': ['Provider classifier and MusiCNN upstream audio overlap remains unresolved.',
                       'Known fit dependencies do not prove independence from unknown upstream training audio.']}


def prepare():
    inventory = audit()
    base = RUN / 'inputs'
    library = json.loads((base / 'library.json').read_text())
    human = json.loads((base / 'human-labels.json').read_text())
    frozen_files = json.loads((base / 'manifest.json').read_text())['files']
    compatible = {json.dumps(row['embedding_signature'], sort_keys=True) for row in human['gold']}
    recordings = {row['id']: row for row in library['recordings']}
    allowed = set(inventory['unlabeled_embedding_recording_ids'])
    candidates, served = defaultdict(list), defaultdict(dict)
    for row in library['sample_attributes']:
        if row['recording_id'] in allowed and row['has_embedding']:
            candidates[row['recording_id']].append(row)
    for row in library['recording_vibes']:
        if row['recording_id'] in allowed:
            served[row['recording_id']][row['vibe']] = row
    prediction_inputs = defaultdict(list)
    for row in library['prediction_inputs']:
        prediction_inputs[row['prediction_id']].append(row['measurement_id'])
    measurements = {row['id']: row for row in library['measurements']}
    predictions = {row['id']: row for row in library['predictions']}
    rows, excluded = [], []
    for recording_id, captures in sorted(candidates.items()):
        points, signature = {}, None
        for capture in sorted(captures, key=lambda row: (row['captured_at'], row['id']), reverse=True):
            if capture['point'] not in ('thirty', 'middle', 'two_thirds') or capture['point'] in points:
                continue
            path = Path(capture['filename'])
            relative = str(path.relative_to(base))
            expected = frozen_files.get(relative)
            if not expected or digest(path) != expected:
                raise ValueError('Captured input changed: ' + relative)
            value = json.loads(path.read_text())
            try:
                current = audio_embedding.signature(value)
                audio_embedding.vector([value])
            except (ValueError, TypeError, KeyError):
                excluded.append({'sample_id': capture['id'], 'reason': 'invalid_embedding'})
                continue
            encoded = json.dumps(current, sort_keys=True)
            if encoded not in compatible or (signature is not None and signature != current):
                excluded.append({'sample_id': capture['id'], 'reason': 'incompatible_embedding_signature'})
                continue
            signature = current
            points[capture['point']] = (capture, value)
        if not points:
            excluded.append({'recording_id': recording_id, 'reason': 'no_compatible_captured_embedding'})
            continue
        selected = sorted(points.values(), key=lambda pair: (pair[1]['position_seconds'], pair[0]['id']))
        scores, complete, contributor_ids, teacher_versions = [], True, set(), set()
        sources = set()
        for vibe in VIBES:
            entry = served[recording_id].get(vibe)
            if entry is None:
                complete = False
                break
            scores.append(entry['score'])
            sources.add(entry['source'])
            teacher_versions.add(entry['inputs_version'])
            if entry['measurement_id'] is not None:
                measurement = measurements.get(entry['measurement_id'])
                if measurement:
                    contributor_ids.add(measurement['id'])
                    teacher_versions.add(measurement['model_version'])
                else:
                    complete = False
            elif entry['prediction_id'] is not None:
                prediction = predictions.get(entry['prediction_id'])
                complete &= bool(prediction and prediction['lineage_complete'])
                links = prediction_inputs[entry['prediction_id']]
                complete &= bool(links) and all(key in measurements for key in links)
                contributor_ids.update(links)
            else:
                # Heuristic/catalog scores have no frozen original contributor.
                complete = False
        recording = recordings[recording_id]
        training_provenance = teacher_training_provenance([measurements[key] for key in contributor_ids
                                                         if key in measurements])
        rows.append({'recording_id': recording_id, 'artist': recording['artist'], 'title': recording['title'],
            'group': identity(recording['artist']) or 'recording:' + str(recording_id),
            'embedding_values': audio_embedding.vector([value for _, value in selected]),
            'embedding_signature': signature, 'sample_ids': [capture['id'] for capture, _ in selected],
            'sample_points': [capture['point'] for capture, _ in selected],
            'sample_positions_seconds': [value['position_seconds'] for _, value in selected],
            'sample_captured_at': [capture['captured_at'] for capture, _ in selected],
            'teacher': {'vibes': list(VIBES), 'scores': scores if len(scores) == len(VIBES) else None,
                'sources': sorted(sources), 'lineage_complete': bool(complete),
                'model_versions': sorted(teacher_versions), 'measurement_ids': sorted(contributor_ids),
                'training_provenance': training_provenance,
                'contributor_recording_ids': sorted({measurements[key]['recording_id'] for key in contributor_ids
                                                    if key in measurements}),
                'target_source': 'stored_model_prediction', 'independent_measurement_votes': 0},
            'human_labels': None})
    output = {'created_at': stamp(), 'input_manifest_sha256': digest(base / 'manifest.json'),
        'script_sha256': digest(__file__), 'rows': rows, 'excluded': excluded,
        'policy': 'Latest compatible actual capture at each available point, averaged once per recording. '
                  'Retain partial coverage and preserve distinct section positions. Stored teacher scores are weak '
                  'targets, with no human-label promotion or additional measurement votes.',
        'limitations': ['Twelve candidates are a small local pilot, not a large acoustic training set.',
                       'Compatibility of embedding metadata does not prove full browser/reference preprocessing parity.',
                       'Artist/teacher/propagation contributors must be excluded separately for each evaluation fold.',
                       'Different song sections are observations of one recording, not invariance augmentation pairs.']}
    save(OUT / 'checkpoint-1/local-audio-pool.json', output)
    print(json.dumps({'local_audio_pool': len(rows), 'complete_soft_teachers': sum(
        row['teacher']['lineage_complete'] for row in rows), 'excluded_captures': len(excluded)}), flush=True)
    return output


if __name__ == '__main__':
    prepare()
