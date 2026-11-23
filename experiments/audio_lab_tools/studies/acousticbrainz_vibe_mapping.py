"""Compare a fitted AcousticBrainz mapping with the current Vibe fill-in."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import zipfile

import numpy as np

from yt_playlist.controllers import vibe_prediction
from yt_playlist.core import metadata_native, metadata_runtime, sample_files
from yt_playlist.rec import (audio_vibe_arc, audio_vibe_runtime, audio_vibe_specialists, home_vibe,
                             musical_profiles)

from ..evaluation import mapped_vibe_metrics
from ..support.external_experiment import ROOT, digest, save, stamp
from ..training import external_training as ridge

VIBES = tuple(vibe for vibe, _ in audio_vibe_specialists.SPECIALISTS)
CLASSIFIERS = ('danceability', 'mood_party', 'mood_aggressive', 'mood_happy', 'mood_sad',
               'mood_relaxed', 'mood_acoustic', 'instrumental')
DESCRIPTORS = {'bpm': 'bpm', 'loudness': 'essentia.average_loudness',
               'dynamic_complexity': 'essentia.dynamic_complexity'}
INPUTS = {'classifiers': CLASSIFIERS, 'with_descriptors': CLASSIFIERS + tuple(DESCRIPTORS)}
FOLDS = 5


def provider_inputs(conn, recording):
    sources = [dict(row) for row in conn.execute('''
        SELECT s.source_key,s.provider,s.scope,s.entity_id FROM rec_musical_links l
        JOIN rec_musical_sources s USING(source_key)
        WHERE l.recording_id=? ORDER BY s.source_key LIMIT 64
    ''', (recording,)) if row['scope'] == 'recording']
    for source in sources:
        source['observations'] = metadata_native.source_values(conn, source.pop('source_key'))
    choices = defaultdict(set)
    for field, value in conn.execute('SELECT ' + metadata_runtime.field_expression('c') + ',json_quote('
            + metadata_runtime.value_expression('c') + ') FROM metadata_runtime_choices c '
            'JOIN catalog_entries e ON e.id=c.entry_id WHERE e.recording_id=? AND c.kind<>0 AND c.field<>3',
            (recording,)):
        choices[field].add(value)
    return sources, {field: json.loads(next(iter(values))) if len(values) == 1 else None
                     for field, values in choices.items()}


def native_features(source):
    features = {}
    units = dict.fromkeys(CLASSIFIERS, 'classifier_probability') | DESCRIPTORS
    for field, observation in source['observations'].items():
        value, provenance = observation['value'], observation['provenance']
        entity = provenance.get('entity_id')
        if (field in units and provenance.get('unit') == units[field]
                and provenance.get('scope') == 'recording'
                and isinstance(entity, str) and entity.lower() == source['entity_id'].lower()
                and type(value) in (float, int) and math.isfinite(value)
                and (field not in CLASSIFIERS or 0 <= value <= 1)):
            features[field] = value
    return features


def former_fill_in(sources, overrides):
    """Replay the retired provider score formula for the comparison."""
    fields = {'bpm', *CLASSIFIERS}
    native = []
    for source in sources:
        observations = {name: value for name, value in source['observations'].items() if name in fields}
        if source['scope'] == 'recording' and observations:
            native.append(musical_profiles.source_profile({**source, 'observations': observations}))
    own = musical_profiles.resolve(native, overrides={key: value for key, value in overrides.items() if key in fields})
    dimensions = own['dimensions']
    if not (any(dimensions[axis]['support'] for axis in ('instrumental', 'relaxed', 'happiness', 'sadness', 'aggression'))
            or any('mood_party' in source['observations'] for source in native)):
        return {}
    return {vibe: home_vibe.heuristic_affinity(own, vibe) for vibe, preset in home_vibe.PRESETS.items()
            if any(dimensions[axis]['support'] and weight for axis, weight in preset['weights'].items())}


def direct_scores(conn, recording, predictor):
    # Reuse the excerpts that backed the selected direct scores in the snapshot.
    rows = conn.execute('''
        SELECT DISTINCT s.* FROM recording_vibes v
        JOIN measurement_samples m USING(measurement_id)
        JOIN sample_attributes s ON s.id=m.sample_id
        WHERE v.recording_id=? AND v.source='audio_measurement'
          AND s.recording_id=v.recording_id AND s.file_status='ready'
        ORDER BY s.point
    ''', (recording,)).fetchall()
    if len(rows) != 3 or len({(row['run_id'], row['video_id']) for row in rows}) != 1:
        return None
    samples = [sample_files.read(row['filename'], digest=row['content_digest'],
               expected={field: row[field] for field in ('id', 'run_id', 'point', 'video_id')}) for row in rows]
    fits = (predictor.predict(samples) or {}).get('suitability', {})
    if not set(VIBES) <= fits.keys():
        return None
    return {vibe: fits[vibe] for vibe in VIBES}, [row['id'] for row in rows]


def capture(conn, predictor):
    population = conn.execute('''
        SELECT r.id,r.mb_recording_id,min(v.artist_key) AS artist_key,
            EXISTS(SELECT 1 FROM recording_identifiers i WHERE i.recording_id=r.id
                AND i.scheme='musicbrainz' AND i.entity_type='recording' AND i.source='musicbrainz'
                AND i.status<>'conflicting' AND lower(i.value)=lower(r.mb_recording_id)) AS corroborated
        FROM recordings r JOIN recording_vibes v ON v.recording_id=r.id
        WHERE v.source='audio_measurement' GROUP BY r.id ORDER BY r.id
    ''').fetchall()
    counts, excluded, examples = Counter(direct_recordings=len(population)), {}, []
    for recording, mbid, artist_key, corroborated in population:
        linked = conn.execute('''
            SELECT lower(s.entity_id) FROM rec_musical_links l JOIN rec_musical_sources s USING(source_key)
            WHERE l.recording_id=? AND s.provider='acousticbrainz' AND s.scope='recording'
        ''', (recording,)).fetchall()
        linked = {row[0] for row in linked}
        if not linked:
            excluded[recording] = 'no_acousticbrainz_link'
            continue
        counts['with_acousticbrainz_link'] += 1
        if not mbid or mbid.lower() not in linked:
            excluded[recording] = 'selected_mbid_differs'
            continue
        counts['selected_mbid_agrees'] += 1
        if not corroborated:
            excluded[recording] = 'no_matching_musicbrainz_observation'
            continue
        artists = set(json.loads(artist_key or '[]'))
        artists.update(row[0] for row in conn.execute('''
            SELECT a.artist_id FROM entry_artists a JOIN catalog_entries e ON e.id=a.entry_id
            WHERE e.recording_id=?
        ''', (recording,)))
        if not artists:
            excluded[recording] = 'missing_artist_credit'
            continue
        sources, overrides = provider_inputs(conn, recording)
        features = next((native_features(source) for source in sources
                         if source['provider'] == 'acousticbrainz'
                         and source['entity_id'].lower() == mbid.lower()), {})
        if not features.keys() & set(CLASSIFIERS):
            excluded[recording] = 'no_native_classifier_values'
            continue
        baseline = former_fill_in(sources, overrides)
        if not set(VIBES) <= baseline.keys():
            excluded[recording] = 'current_fill_in_missing'
            continue
        scored = direct_scores(conn, recording, predictor)
        if scored is None:
            excluded[recording] = 'no_complete_compatible_sample_run'
            continue
        scores, samples = scored
        examples.append({'recording_id': recording, 'mbid': mbid.lower(), 'artists': sorted(artists),
                         'samples': samples, 'inputs': features, 'direct': scores,
                         'current': {vibe: baseline[vibe] for vibe in VIBES}})
    counts['paired_recordings'] = len(examples)
    counts['paired_artists'] = len({artist for row in examples for artist in row['artists']})
    counts['paired_mbids'] = len({row['mbid'] for row in examples})
    return {'counts': dict(counts), 'excluded': excluded,
            'field_counts': dict(Counter(field for row in examples for field in row['inputs'])),
            'exclusion_counts': dict(Counter(excluded.values())), 'examples': examples}


def evaluate(examples):
    groups = np.array(ridge.validation_groups([
        {'id': row['mbid'], 'group': row['recording_id'],
         'artist': '+'.join(f'credit {artist}' for artist in row['artists'])} for row in examples]))
    split = ridge.folds(groups, FOLDS)
    observed = np.array([[row['direct'][vibe] for vibe in VIBES] for row in examples])
    current = np.array([[row['current'][vibe] for vibe in VIBES] for row in examples])
    predictions, alphas = {'current': current}, {}
    for method, fields in INPUTS.items():
        x = np.array([[row['inputs'].get(field) for field in fields] for row in examples], dtype=float)
        fitted = np.empty_like(observed)
        alphas[method] = {}
        for fold in range(FOLDS):
            train, test = split != fold, split == fold
            train_groups = groups[train]
            alphas[method][fold] = {}
            for index, vibe in enumerate(VIBES):
                y = observed[train, index:index+1]
                strength = ridge.choose_alpha(x[train], y, train_groups, [0])
                model, = ridge.fit_path(x[train], y, (strength,))
                fitted[test, index] = ridge.predict(model, x[test])[:, 0]
                alphas[method][fold][vibe] = strength
        predictions[method] = fitted
    report = {}
    for index, vibe in enumerate(VIBES):
        y = observed[:, index]
        report[vibe] = {'direct_range': [float(y.min()), float(y.max())], 'results': {}}
        for method, values in predictions.items():
            report[vibe]['results'][method] = {
                **mapped_vibe_metrics.summary(y, values[:, index], split),
                'mae': float(np.mean(np.abs(y - values[:, index]))),
            }
    held_out = [{'recording_id': row['recording_id'], 'group': int(groups[i]), 'fold': int(split[i]),
                 'direct': row['direct'], **{method: dict(zip(VIBES, values[i].tolist(), strict=True))
                 for method, values in predictions.items()}} for i, row in enumerate(examples)]
    return {'groups': len(set(groups)), 'fold_counts': np.bincount(split).tolist(),
            'selected_alphas': alphas, 'vibes': report, 'held_out': held_out}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--baseline-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    predictor = vibe_prediction.configured().capture()
    models = {vibe: model['sha256'] for vibe, mode, model, _ in predictor.specialists if mode == 'active'}
    if models.keys() != set(VIBES):
        raise ValueError('All six active specialist artifacts are required to rescore saved audio')
    sources = {Path(module.__file__).resolve().relative_to(ROOT) for module in (
        vibe_recording_rules, vibe_prediction, metadata_native, metadata_runtime, sample_files,
        mapped_vibe_metrics, ridge, audio_vibe_arc, audio_vibe_runtime, audio_vibe_specialists,
        home_vibe, musical_profiles, *(module for _, module in audio_vibe_specialists.SPECIALISTS))}
    sources.add(Path(__file__).resolve().relative_to(ROOT))
    with zipfile.ZipFile(output / 'source.zip', 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(sources):
            archive.write(ROOT / source, source)
    save(output / 'plan.json', {'created_at': stamp(), 'baseline_id': args.baseline_id, 'inputs': INPUTS,
         'specialists': models, 'measurement_version': vibe_recording_rules.version(),
         'comparator': 'Production provider fill-in with audio hidden and original overrides',
         'outer_folds': FOLDS, 'seed': ridge.SEED, 'alphas': ridge.ALPHAS,
         'source_sha256': digest(output / 'source.zip')})
    with closing(sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        dataset = capture(conn, predictor)
    save(output / 'dataset.json', dataset)
    print(json.dumps({key: dataset[key] for key in ('counts', 'exclusion_counts')}), flush=True)
    result = evaluate(dataset['examples'])
    result['dataset_sha256'] = digest(output / 'dataset.json')
    save(output / 'comparison.json', result)
    print(json.dumps({vibe: {method: {key: scores[key] for key in ('mae', 'ndcg', 'top_mean')}
                     for method, scores in row['results'].items()} for vibe, row in result['vibes'].items()}), flush=True)


if __name__ == '__main__':
    main()
