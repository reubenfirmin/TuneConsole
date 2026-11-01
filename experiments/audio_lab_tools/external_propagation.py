"""Replay the production propagation policy on frozen original measurements.

No database connection, native inference, or propagated training votes. This
isolates cohort behavior; it does not claim complete Radio/Mix Tape acceptance.
"""
from collections import Counter
import hashlib
import json
import time

import numpy as np
from threadpoolctl import threadpool_limits

from yt_playlist.rec import vibe_prediction as policy

from .external_checkpoint import RUN
from .external_experiment import OUT, digest, save, stamp
from .external_secondary import identity
from .external_training import SEED, folds
from .external_transfer import SNAPSHOT, VIBES


def current_inputs(library, sources):
    measurements = {row['id']: row for row in library['measurements']}
    metadata, observations = {}, {}
    for row in library['recording_vibes']:
        rid = row['recording_id']
        keys = {'artist': row['artist_key'], 'genre': row['subgenre_key']}
        if rid in metadata and metadata[rid] != keys:
            raise ValueError('Current cohort metadata differs between vibes')
        metadata[rid] = keys
        if row['measurement_id'] is None or row['source'] not in sources:
            continue
        measurement = measurements[row['measurement_id']]
        if (measurement['recording_id'] != rid or measurement['vibe'] != row['vibe']
                or measurement['source'] != row['source']):
            raise ValueError('Current measurement identity does not match serving reference')
        if measurement['support'] <= 0:
            continue
        evidence = observations.setdefault(rid, {'source': row['source'], 'fits': {}, 'measurement_ids': {}})
        if row['vibe'] in evidence['fits']:
            raise ValueError('A recording has duplicate current measurement votes')
        evidence['fits'][row['vibe']] = {key: measurement[key] for key in ('score', 'support')}
        evidence['measurement_ids'][row['vibe']] = measurement['id']
    return metadata, observations


def cohorts(metadata, observations, excluded=()):
    excluded = set(excluded)
    result = {}
    for rid, observation in observations.items():
        if rid in excluded:
            continue
        for key in policy.groups(**metadata[rid]):
            result[key] = policy.adjust(result.get(key, {}), observation, 1)
    return result


def resolve(metadata, statistics):
    candidates = [(key, statistics.get(key, {})) for key in policy.groups(**metadata)]
    # Frozen inputs do not include the catalog-only fallback profile. A neutral
    # fallback avoids leaking the hidden recording's captured audio heuristic.
    return policy.resolve(dict.fromkeys(VIBES, .5), None, candidates)['fits']


def artist_ids(metadata):
    return set(json.loads(metadata.get('artist') or '[]'))


def human_groups(rows, recording_ids, metadata):
    parents, seen = list(range(len(rows))), {}

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    for i, (row, rid) in enumerate(zip(rows, recording_ids)):
        keys = [('recording', rid)] + [('artist', key) for key in artist_ids(metadata[rid])]
        if name := identity(row['artist']):
            keys.append(('name', name))
        for key in keys:
            if key in seen:
                parents[root(i)] = root(seen[key])
            else:
                seen[key] = i
    return [str(root(i)) for i in range(len(rows))]


def human_diagnostic(library, human, metadata, observations):
    from .external_binary_calibration import metrics

    # Keep all twenty newer labels reserved. These historical labels have been
    # examined repeatedly; only provider measurements seed this diagnostic.
    historical = json.loads(SNAPSHOT.read_text())
    historical_rows = [row for row in historical['gold'] if len(row.get('embedding_values', [])) == 200]
    samples = {row['id']: row for row in library['sample_attributes']}
    current = {row['video_id']: row for row in human['gold']}
    rows, recording_ids, excluded = [], [], []
    for row in historical_rows:
        captured = current.get(row['video_id'], {})
        ids = {samples[key]['recording_id'] for key in captured.get('sample_ids', []) if key in samples}
        if len(ids) != 1 or next(iter(ids)) not in metadata:
            excluded.append(row['video_id'])
            continue
        rows.append(row)
        recording_ids.append(next(iter(ids)))
    split = folds(human_groups(rows, recording_ids, metadata))
    predictions = {'neutral_fallback': np.full((len(rows), len(VIBES)), .5)}
    audits = {}
    for mode in ('recording_heldout', 'artist_heldout'):
        values, coverage, partition_audit = np.zeros((len(rows), len(VIBES))), [], []
        for fold in range(5):
            valid = split == fold
            hidden = {rid for rid, keep in zip(recording_ids, valid) if keep}
            if mode == 'artist_heldout':
                artists = set().union(*(artist_ids(metadata[rid]) for rid in hidden))
                hidden |= {rid for rid in observations if artist_ids(metadata[rid]) & artists}
            statistics = cohorts(metadata, observations, hidden)
            for i in np.flatnonzero(valid):
                fits = resolve(metadata[recording_ids[i]], statistics)
                values[i] = [fits[name]['score'] for name in VIBES]
                coverage.append(any(fits[name]['source'] != 'heuristic' for name in VIBES))
            partition_audit.append({'fold': fold, 'excluded_recording_ids': sorted(hidden),
                                    'retained_original_recordings': len(set(observations) - hidden)})
        predictions[mode] = values
        audits[mode] = {'recordings_with_prediction': sum(coverage), 'folds': partition_audit}
    y = np.asarray([[int(name in row['labels']) for name in VIBES] for row in rows])
    save(RUN / 'propagation-human.oof.json', {'ids': [row['video_id'] for row in rows],
        'recording_ids': recording_ids, 'folds': split.tolist(), 'observed': y.tolist(),
        'predictions': {name: value.tolist() for name, value in predictions.items()}})
    return {'rows': len(rows), 'excluded_historical_ids': excluded,
        'methods': {name: metrics(y, value) for name, value in predictions.items()},
        'partition_audit': audits, 'seed_sources': ['provider_measurement'],
        'historical_snapshot_sha256': digest(SNAPSHOT),
        'limitations': ['Previously inspected developer labels are exploratory; unselected alternatives are not explicit rejections.',
                       'A neutral fallback isolates propagation because the frozen export lacks catalog-only fallback profiles.',
                       'Provider classifiers are model outputs, not additional human ratings. Their upstream overlap remains unresolved.']}


def measurement_consistency(metadata, observations):
    eligible = [rid for rid, row in observations.items() if set(row['fits']) == set(VIBES)]
    groups = [metadata[rid]['artist'] or 'recording:' + str(rid) for rid in eligible]
    split = folds(groups)
    observed, predicted, coverage = [], [], []
    for fold in range(5):
        hidden = {rid for rid, keep in zip(eligible, split == fold) if keep}
        artists = set().union(*(artist_ids(metadata[rid]) for rid in hidden))
        hidden |= {rid for rid in observations if artist_ids(metadata[rid]) & artists}
        statistics = cohorts(metadata, observations, hidden)
        for rid, keep in zip(eligible, split == fold):
            if keep:
                fits = resolve(metadata[rid], statistics)
                observed.append([observations[rid]['fits'][name]['score'] for name in VIBES])
                predicted.append([fits[name]['score'] for name in VIBES])
                coverage.append([fits[name]['source'] != 'heuristic' for name in VIBES])
    y, values = np.asarray(observed), np.asarray(predicted)
    return {'recordings': len(eligible), 'mae': float(np.abs(y - values).mean()),
        'neutral_fallback_mae': float(np.abs(y - .5).mean()),
        'predicted_recording_fraction': float(np.any(coverage, axis=1).mean()),
        'per_vibe_mae': dict(zip(VIBES, np.abs(y - values).mean(0).tolist())),
        'validation': 'Five artist-credit grouped folds; remove held-out recordings and all contributors sharing any canonical artist ID.',
        'interpretation': 'Agreement with withheld model measurements is consistency evidence, not independent human accuracy.'}


def coverage_curve(metadata, observations):
    ordered = sorted(observations, key=lambda rid: hashlib.sha256(f'{SEED}:{rid}'.encode()).hexdigest())
    rows = []
    for count in sorted({0, 1, 8, 32, 128, 512, len(ordered)}):
        if count > len(ordered):
            continue
        selected = {rid: observations[rid] for rid in ordered[:count]}
        started = time.monotonic()
        statistics = cohorts(metadata, selected)
        kinds, predicted = Counter(), 0
        for rid, fields in metadata.items():
            if rid in selected:
                continue
            fits = resolve(fields, statistics)
            kinds.update(fit['source'] for fit in fits.values())
            predicted += any(fit['source'] != 'heuristic' for fit in fits.values())
        rows.append({'original_seed_recordings': count, 'unmeasured_recordings': len(metadata) - count,
            'unmeasured_recordings_with_prediction': predicted, 'resolved_vibes_by_source': dict(kinds),
            'policy_replay_seconds': round(time.monotonic() - started, 4)})
    return {'rows': rows, 'policy': 'Seeded identity order independent of labels and prediction values; every seed is one original recording.',
        'interpretation': 'Coverage as evidence accumulates; policy runtime excludes browser sampling, preparation, database I/O and app startup.'}


def retraction_check(metadata, observations):
    rid = next(iter(observations))
    original = cohorts(metadata, observations)
    revised = {key: value for key, value in original.items()}
    for key in policy.groups(**metadata[rid]):
        revised[key] = policy.adjust(revised[key], observations[rid], -1)
    rebuilt = cohorts(metadata, observations, {rid})
    error = max((abs(a - b) for key in set(revised) | set(rebuilt)
                 for name in set(revised.get(key, {})) | set(rebuilt.get(key, {}))
                 for a, b in zip(revised.get(key, {}).get(name, [0] * 4),
                                 rebuilt.get(key, {}).get(name, [0] * 4))), default=0.)
    if error > 1e-8:
        raise ValueError('Incremental measurement retraction differs from a full rebuild')
    return {'recording_id': rid, 'maximum_statistic_error': error,
            'missing_metadata_sources': sorted({fit['source'] for fit in resolve(
                {'artist': None, 'genre': None}, original).values()}),
            'independent_votes_per_recording': 1, 'propagated_recordings_added_as_votes': 0}


def evaluate():
    base = RUN / 'inputs'
    manifest = json.loads((base / 'manifest.json').read_text())
    for name in ('library.json', 'human-labels.json'):
        if digest(base / name) != manifest['files'][name]:
            raise ValueError('Frozen propagation input changed: ' + name)
    library = json.loads((base / 'library.json').read_text())
    human = json.loads((base / 'human-labels.json').read_text())
    metadata, observations = current_inputs(library, {'provider_measurement', 'audio_measurement'})
    _, provider = current_inputs(library, {'provider_measurement'})
    report = {'created_at': stamp(), 'input_manifest_sha256': digest(base / 'manifest.json'),
        'script_sha256': digest(__file__), 'policy_sha256': digest(policy.__file__),
        'recordings': len(metadata), 'original_measurement_recordings': len(observations),
        'provider_seed_recordings': len(provider),
        'human_diagnostic': human_diagnostic(library, human, metadata, provider),
        'measurement_consistency': measurement_consistency(metadata, observations),
        'coverage': coverage_curve(metadata, observations),
        'retraction': retraction_check(metadata, observations),
        'limitations': ['This replays the actual pure propagation policy, not the complete preparation or listening workflow.',
                       'Frozen artist/genre membership omits missing or later metadata changes.',
                       'Stored audio measurements include teachers fitted on historical local labels; they enter consistency and coverage only.',
                       'Current propagated descendants and incomplete historical lineage never seed recomputed cohorts.',
                       'Radio/Mix Tape output, actual startup latency, browser costs and listening judgments remain separate checks.']}
    save(RUN / 'propagation.report.json', report)
    print(json.dumps({'propagation_recordings': len(metadata), 'provider_seed_recordings': len(provider),
        'human_methods': {name: value['macro_average_precision']
                          for name, value in report['human_diagnostic']['methods'].items()},
        'measurement_consistency': report['measurement_consistency']}), flush=True)
    return report


if __name__ == '__main__':
    with threadpool_limits(limits=1):
        evaluate()
