"""Compare reusable Emotify heads on identical held-out developer songs.

Reads immutable sample files through a read-only database connection. Writes
research reports only; never changes the application model or user database.
"""
import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
from statistics import median

import numpy as np
from threadpoolctl import threadpool_limits

from yt_playlist.core import audio_embedding, audio_vibe_metadata, sample_files
from yt_playlist.rec import audio_vibe_external, audio_vibe_model, home_vibe, musical_profiles

from .external_experiment import OUT, digest, normalize, save, stamp
from .external_ranking import average_precision
from .external_training import choose_alpha, feature_matrices, fit_path, folds, metrics, predict
from .external_transfer import SNAPSHOT, VIBES, probabilities, score, select_vibe_alpha

SHARED = ('bpm', 'mood_relaxed', 'mood_aggressive', 'mood_happy', 'mood_sad')
MOODS = {'mood_relaxed': 'relaxed', 'mood_aggressive': 'aggressive',
         'mood_happy': 'happy', 'mood_sad': 'sad'}


def sample_moods(sample, names):
    return [(sample['traits'][name]['score'] - (1 if name in ('arousal', 'valence') else 0)) /
            (8 if name in ('arousal', 'valence') else 1) for name in names]


def capture_inputs(samples, attribute_names, mood_names):
    """Same native units and mood normalization as the external feature reader."""
    attributes = [median(sample['audio_attributes']['values'][key] for sample in samples)
                  for key in attribute_names]
    moods = np.median([sample_moods(sample, mood_names) for sample in samples], axis=0)
    return attributes, moods


def families(attributes, embedding, moods, shared):
    return {'attributes': np.asarray(attributes, dtype=float),
            'embedding': np.asarray(embedding, dtype=float),
            'pretrained_moods': np.asarray(moods, dtype=float),
            'provider_shared': np.asarray(shared, dtype=float),
            'attributes_embedding': np.column_stack([attributes, embedding]),
            'attributes_moods': np.column_stack([attributes, moods]),
            'attributes_embedding_moods': np.column_stack([attributes, embedding, moods])}


def gold_inputs(snapshot, conn, attribute_names, mood_names, *, sample_root, sample_index):
    rows, attributes, moods, shared, files = [], [], [], [], []
    missing = []
    for row in snapshot['gold']:
        if len(row.get('embedding_values', [])) != 200:
            missing.append({'video_id': row['video_id'], 'reason': 'no_compatible_embedding'})
            continue
        samples = []
        for sample_id in row['sample_ids']:
            if sample_id in sample_index:
                sha256 = sample_index[sample_id]
                filename = sample_root / sha256[:2] / (sha256 + '.json')
            else:
                stored = conn.execute('SELECT filename FROM sample_attributes WHERE id=?', (sample_id,)).fetchone()
                if stored is None:
                    raise ValueError('Frozen gold sample not present: ' + sample_id)
                filename = Path(stored[0])
            sample = sample_files.read(filename, expected={'id': sample_id, 'video_id': row['video_id']})
            if audio_embedding.signature(sample) != row['embedding_signature']:
                raise ValueError('Gold sample embedding signature differs from frozen snapshot')
            samples.append(sample)
            files.append({'sample_id': sample_id, 'sha256': digest(filename)})
        if not np.allclose(audio_embedding.vector(samples), row['embedding_values'], atol=1e-12, rtol=0):
            raise ValueError('Frozen gold embedding does not replay from its immutable samples')
        values, traits = capture_inputs(samples, attribute_names, mood_names)
        frozen = dict(zip(audio_vibe_model.FEATURES, row['values'], strict=True))
        provider = [frozen['base.' + key] for key in SHARED]
        rows.append(row)
        attributes.append(values)
        moods.append(traits)
        shared.append([np.nan if value is None else value for value in provider])
    x = families(attributes, [row['embedding_values'] for row in rows], moods, shared)
    return rows, x, files, missing


def add_ranking_metrics(target, prediction, *, normalize_scores=True):
    result = score(target, prediction)
    p = probabilities(prediction) if normalize_scores else np.asarray(prediction)
    result['acceptable_songs'] = int(((target > 0)[np.arange(len(p)), p.argmax(axis=1)]).sum())
    result['average_precision'] = {name: average_precision(target[:, index] > 0, p[:, index])
                                   for index, name in enumerate(VIBES)}
    available = [value for value in result['average_precision'].values() if value is not None]
    result['macro_average_precision'] = float(np.mean(available)) if available else None
    return result


def evaluate(db):
    snapshot = json.loads(SNAPSHOT.read_text())
    source_path = OUT / 'emotify.features.json'
    native = json.loads(source_path.read_text())['rows']
    original, names, moods, mood_names = feature_matrices(native)
    attributes = original['attributes']
    shared = np.column_stack([attributes[:, names['attributes'].index('tempo_bpm')],
                              *[moods[:, mood_names.index(MOODS[key])] for key in SHARED[1:]]])
    nx = families(attributes, original['embedding'], moods, shared)
    previous_path = OUT / 'emotify-input-comparison.report.json'
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    sample_index = {row['sample_id']: row['sha256'] for row in previous.get('sample_files', [])}
    required = {sid for row in snapshot['gold'] if len(row.get('embedding_values', [])) == 200 for sid in row['sample_ids']}
    context = (nullcontext(None) if required <= sample_index.keys() else
               sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5))
    with context as conn:
        if conn is not None:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
        gold, gx, sample_hashes, missing = gold_inputs(snapshot, conn, names['attributes'], mood_names,
            sample_root=db.resolve().parent / 'sample-attributes', sample_index=sample_index)
    native_example = json.loads(Path(native[0]['feature_file']).read_text())['prediction']
    native_signature = audio_embedding.signature({**native_example, 'artifact_hashes': {
        file['path']: file['sha256'] for file in native_example['bundle']['files']}})
    if {row['embedding_signature'] for row in gold} != {native_signature}:
        raise ValueError('External and gold extractor provenance differs')
    target = np.array([row['targets'] for row in native], dtype=float)
    groups = [row['group'] for row in native]
    native_folds = folds(groups)
    gold_groups = [normalize(row['artist']) or row['video_id'] for row in gold]
    gold_folds = folds(gold_groups)
    gy = np.array([[int(vibe in row['labels']) / len(row['labels']) for vibe in VIBES] for row in gold])
    result = {'created_at': stamp(), 'snapshot_sha256': digest(SNAPSHOT),
              'external_index_sha256': digest(source_path), 'script_sha256': digest(__file__),
              'gold_rows': len(gold), 'native_rows': len(native), 'silver_rows_excluded': len(snapshot['silver']),
              'excluded_gold': missing, 'embedding_signature': native_signature,
              'sample_files': sample_hashes,
              'folds': [{'video_id': row['video_id'], 'artist_group': group, 'fold': int(fold)}
                        for row, group, fold in zip(gold, gold_groups, gold_folds)],
              'features': {'attributes': names['attributes'], 'pretrained_moods': mood_names,
                           'provider_shared': SHARED},
              'native': {}, 'transfer': {}, 'direct_gold': {}, 'limitations': [
                  'Exploratory comparison on 55 previously examined developer songs, not population accuracy.',
                  'Native outer folds separate recordings; Emotify artist identities are unavailable.',
                  'Gold outer folds separate normalized artist strings; aliases can overlap.',
                  'Provider-shared transfer crosses extractor domains: browser MusiCNN moods and provider mood heads differ.',
                  'Provider-only results use frozen catalog fields, never copied captured moods or embeddings.',
                  'No mapping between provider loudness/energy and incompatible sampled-audio units is assumed.',
                  'Native audio uses a single excerpt; gold attributes and moods use medians across saved excerpts.',
                  'All scaling, imputation and calibration selection fit training folds only.',
                  'External native heads use all Emotify labels but no developer labels.',
                  'No product database writes, model changes, or silver fitting.']}
    result['provider_availability_on_gold'] = {
        key: int(np.isfinite(gx['provider_shared'][:, index]).sum()) for index, key in enumerate(SHARED)}
    result['provider_availability_on_gold']['all_shared_fields'] = int(np.isfinite(gx['provider_shared']).all(axis=1).sum())
    result['provider_availability_on_gold']['any_shared_field'] = int(np.isfinite(gx['provider_shared']).any(axis=1).sum())
    predictions = {}
    for name, x in nx.items():
        native_oof = np.zeros_like(target)
        alphas = []
        for fold in range(5):
            train, test = native_folds != fold, native_folds == fold
            alpha = choose_alpha(x[train], target[train], np.array(groups)[train].tolist(), list(range(target.shape[1])))
            native_oof[test] = predict(fit_path(x[train], target[train], [alpha])[0], x[test])
            alphas.append(alpha)
        result['native'][name] = {**metrics(target, native_oof, native[0]['target_names']), 'alpha_by_fold': alphas}
        alpha = choose_alpha(x, target, groups, list(range(target.shape[1])))
        head = fit_path(x, target, [alpha])[0]
        transferred = predict(head, gx[name])
        for kind, inputs in (('transfer', transferred), ('direct_gold', gx[name])):
            oof = np.zeros_like(gy)
            calibration_alphas = []
            for fold in range(5):
                train, test = gold_folds != fold, gold_folds == fold
                if set(np.array(gold_groups)[train]) & set(np.array(gold_groups)[test]):
                    raise AssertionError('Artist leakage')
                penalty = select_vibe_alpha(inputs[train], gy[train], np.array(gold_groups)[train].tolist())
                oof[test] = predict(fit_path(inputs[train], gy[train], [penalty])[0], inputs[test])
                calibration_alphas.append(penalty)
            result[kind][name] = {**add_ranking_metrics(gy, oof), 'alpha_by_fold': calibration_alphas}
            if kind == 'transfer':
                result[kind][name]['native_head_alpha'] = alpha
            predictions[kind + ':' + name] = probabilities(oof).tolist()
        print(json.dumps({'family': name, 'native_mae': result['native'][name]['mae'],
                          'transfer': result['transfer'][name], 'direct_gold': result['direct_gold'][name]}), flush=True)
    result['heuristic'] = add_ranking_metrics(gy, np.array([[row['baseline'][vibe] for vibe in VIBES] for row in gold]),
                                             normalize_scores=False)
    save(OUT / 'emotify-input-comparison.report.json', result)
    save(OUT / 'emotify-input-comparison.predictions.json', {'vibes': VIBES,
         'video_ids': [row['video_id'] for row in gold], 'predictions': predictions})
    print(json.dumps({'report': str(OUT / 'emotify-input-comparison.report.json'),
                      'provider_availability_on_gold': result['provider_availability_on_gold']}), flush=True)
    return result


def summarize(db):
    """Add availability and serving evidence without refitting saved trials."""
    report = json.loads((OUT / 'emotify-input-comparison.report.json').read_text())
    saved = json.loads((OUT / 'emotify-input-comparison.predictions.json').read_text())
    snapshot = json.loads(SNAPSHOT.read_text())
    lookup = {row['video_id']: row for row in snapshot['gold']}
    gold = [lookup[video] for video in saved['video_ids']]
    target = np.array([[int(vibe in row['labels']) / len(row['labels']) for vibe in VIBES] for row in gold])
    frozen = [dict(zip(audio_vibe_model.FEATURES, row['values'], strict=True)) for row in gold]
    provider_x = np.array([[np.nan if (value := row['base.' + key]) is None else value for key in SHARED]
                           for row in frozen], dtype=float)
    provider_scores = []
    for row in frozen:
        metadata = {key: row['base.' + key] for key in audio_vibe_metadata.NUMERIC}
        profile = musical_profiles.resolve([], selected=metadata)
        provider_scores.append([home_vibe.heuristic_affinity(profile, vibe) for vibe in VIBES])
    provider_scores = np.array(provider_scores)
    report['provider_eligible_subsets'] = {}
    for name, mask in (('any_shared_field', np.isfinite(provider_x).any(axis=1)),
                       ('all_shared_fields', np.isfinite(provider_x).all(axis=1))):
        report['provider_eligible_subsets'][name] = {
            'transfer': add_ranking_metrics(target[mask], np.array(saved['predictions']['transfer:provider_shared'])[mask]),
            'direct_gold': add_ranking_metrics(target[mask], np.array(saved['predictions']['direct_gold:provider_shared'])[mask]),
            'provider_heuristic': add_ranking_metrics(target[mask], provider_scores[mask], normalize_scores=False)}
    report['captured_audio_heuristic'] = add_ranking_metrics(target, np.array([
        [row['baseline'][vibe] for vibe in VIBES] for row in gold]), normalize_scores=False)
    report.pop('heuristic', None)
    model = json.loads(audio_vibe_model.DEFAULT_PATH.read_text())
    model_hash = digest(audio_vibe_model.DEFAULT_PATH)
    with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        library = conn.execute('SELECT recording_id,MIN(video_id) video_id FROM tracks '
                               'WHERE video_id IS NOT NULL GROUP BY recording_id').fetchall()
        availability = {key: 0 for key in SHARED}
        all_shared = any_shared = 0
        for row in library:
            metadata = audio_vibe_metadata.read(conn, row['video_id'])
            values = dict(zip(audio_vibe_metadata.FEATURES, audio_vibe_metadata.vector(metadata), strict=True))
            present = [values['base.' + key] is not None for key in SHARED]
            for key, valid in zip(SHARED, present): availability[key] += int(valid)
            all_shared += all(present)
            any_shared += any(present)
        counts = conn.execute('SELECT source,COUNT(DISTINCT recording_id) n FROM recording_vibes '
                              'WHERE recording_id IN (SELECT recording_id FROM tracks) GROUP BY source').fetchall()
        version_counts = conn.execute('SELECT source,COUNT(DISTINCT recording_id) n FROM recording_vibes '
            'WHERE recording_id IN (SELECT recording_id FROM tracks) AND inputs_version LIKE ? GROUP BY source',
            ('%/' + model_hash,)).fetchall()
        runs = conn.execute('WITH complete AS (SELECT recording_id,video_id,run_id,MAX(captured_at) captured '
            'FROM sample_attributes GROUP BY recording_id,video_id,run_id HAVING COUNT(*)=3), ranked AS ('
            'SELECT *,ROW_NUMBER() OVER (PARTITION BY recording_id ORDER BY captured DESC,video_id DESC,run_id DESC) n FROM complete) '
            'SELECT * FROM ranked WHERE n=1 AND recording_id IN (SELECT recording_id FROM tracks)').fetchall()
        compatible = matched = stale = invalid = 0
        maximum_error = 0.
        for run in runs:
            try:
                samples = [sample_files.read(row['filename'], expected={'run_id': run['run_id'], 'id': row['id']})
                           for row in conn.execute('SELECT id,filename FROM sample_attributes WHERE run_id=?', (run['run_id'],))]
                expected = audio_vibe_external.scores(model, samples)
            except (OSError, ValueError, KeyError):
                invalid += 1
                continue
            compatible += 1
            current = conn.execute('SELECT vibe,score,source,inputs_version FROM recording_vibes WHERE recording_id=?',
                                   (run['recording_id'],)).fetchall()
            if len(current) == 6 and all(row['source'] == 'audio_measurement' and row['inputs_version'].endswith('/' + model_hash)
                                         for row in current):
                error = max(abs(row['score'] - expected[row['vibe']]) for row in current)
                maximum_error = max(maximum_error, error)
                matched += int(error < 1e-12)
            else: stale += 1
        report['actual_library'] = {'observed_at': stamp(), 'library_recordings_with_video': len(library),
            'provider_shared_field_counts': availability, 'all_shared_fields': all_shared, 'any_shared_field': any_shared,
            'prepared_source_counts': {row['source']: row['n'] for row in counts},
            'current_model_version_source_counts': {row['source']: row['n'] for row in version_counts},
            'complete_capture_recordings': len(runs), 'compatible_capture_recordings': compatible,
            'incompatible_capture_recordings': invalid, 'prepared_current_model_matches': matched,
            'prepared_stale_or_missing': stale, 'maximum_current_model_score_error': maximum_error,
            'bundled_model_sha256': model_hash}
    report['limitations'].append('Heuristic ranking uses its raw served fits; learned model ranking uses normalized exported scores.')
    report['summary_script_sha256'] = digest(__file__)
    save(OUT / 'emotify-input-comparison.report.json', report)
    print(json.dumps({'actual_library': report['actual_library'],
                      'provider_eligible_subsets': report['provider_eligible_subsets'],
                      'captured_audio_heuristic': report['captured_audio_heuristic']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--summarize', action='store_true', help='Add read-only availability evidence to saved results')
    args = parser.parse_args()
    with threadpool_limits(limits=1):
        if args.summarize:
            summarize(args.db)
        else:
            evaluate(args.db)
