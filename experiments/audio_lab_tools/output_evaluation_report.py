"""Reference coverage and paired positive-only diagnostics for #587."""
import argparse
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys

import numpy as np

from .external_experiment import ROOT, digest, save
from .output_evaluation import ARMS, SEED, configure, inside
from . import replay_runtime


def activate_runtime(directory):
    """The reporter imports the same immutable product code as the three arms."""
    plan = json.loads((directory / 'plan.json').read_text())
    target = Path(plan['runtime_snapshot'])
    replay_runtime.verify(target)
    sys.path[:0] = [str(target / 'src'), str(target), str(ROOT)]
    os.environ[replay_runtime.ENVIRONMENT] = str(target)
    if replay_runtime.runtime_hashes() != plan['runtime_files']:
        raise ValueError('Reporter loaded a different runtime')


def verify_route(prediction, shared, specialists, *, compatible):
    routed = {v: m['sha256'] for v, m in prediction.get('individual_models', {}).items()}
    if compatible:
        if prediction.get('model', {}).get('sha256') != shared or routed != specialists:
            raise ValueError('Compatible capture did not use all pinned specialists')
    elif prediction.get('model') or routed:
        raise ValueError('Incompatible capture unexpectedly used a learned model')
    return routed


def inference_audit(directory, run, profiles):
    """Verify specialist routing and legacy audio fallback without a database reader."""
    from pytest import MonkeyPatch
    from yt_playlist.core import audio_embedding, sample_files
    from yt_playlist.rec import audio_vibe_prediction
    plan = json.loads((directory / 'plan.json').read_text())
    samples = {r['id']: r for r in json.loads((directory / 'samples.json').read_text())['rows']}
    joins = json.loads((run / 'capture-joins.json').read_text())
    expected = {name.rsplit('_', 1)[-1]: model['sha256'] for name, model in plan['models'].items()}
    expected['concentrate'] = expected.pop('focus')
    shared = expected.pop('model')
    shared_artifact = json.loads(Path(plan['models']['yt_playlist.rec.audio_vibe_model']['path']).read_text())
    maximum, rows = 0., {}
    with MonkeyPatch.context() as patch:
        configure(patch, directory / 'report-environment')
        for key, ids in joins.items():
            captured = [sample_files.read(samples[sid]['filename'], expected={'id': sid}) for sid in ids]
            reason = None
            try:
                compatible = all(audio_embedding.signature(sample) == shared_artifact['embedding_signature']
                                 for sample in captured)
                if not compatible:
                    reason = 'Embedding provenance differs from the pinned shared model'
            except ValueError as error:
                compatible, reason = False, str(error)
            prediction = audio_vibe_prediction.predict(captured, metadata={})
            routed = verify_route(prediction, shared, expected, compatible=compatible)
            if not compatible and prediction != audio_vibe_prediction.predict(captured, use_model=False):
                raise ValueError('Legacy capture did not retain the exact audio heuristic: ' + key)
            shared_scores = {row['vibe']: row['score'] for row in prediction['ranking']}
            for vibe, score in (prediction.get('suitability') or shared_scores).items():
                maximum = max(maximum, abs(score - profiles[key]['vibes']['fits'][vibe]['score']))
            for vibe in ('energize', 'release'):
                maximum = max(maximum, abs(shared_scores[vibe] - profiles[key]['vibes']['intensity_fits'][vibe]['score']))
            rows[key] = {'sample_ids': ids, 'shared_model': shared if compatible else None,
                         'specialists': routed, 'route': 'six_specialists' if compatible else 'audio_heuristic',
                         'fallback_reason': reason, 'excerpts_without_embedding': sum('embedding' not in s for s in captured)}
    if maximum > 1e-12:
        raise ValueError('Direct consumer profiles do not replay from their exact captures')
    save(directory / 'inference-audit.json', {'rows': rows, 'maximum_score_error': maximum})
    return {'complete_recordings': len(rows), 'routes': dict(Counter(r['route'] for r in rows.values())),
            'fallback_reasons': dict(Counter(r['fallback_reason'] for r in rows.values() if r['fallback_reason'])),
            'maximum_score_error': maximum,
            'sha256': digest(directory / 'inference-audit.json')}


def connected_groups(rows):
    """Connect recording, canonical artists and canonical albums transitively."""
    parents, seen = list(range(len(rows))), {}
    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    for i, row in enumerate(rows):
        keys = [('recording', row['key'])]
        keys += [('artist', str(value)) for value in row.get('artists', [])]
        keys += [('album', str(value)) for value in row.get('albums', [])]
        for key in keys:
            if key in seen:
                parents[root(i)] = root(seen[key])
            else:
                seen[key] = i
    return [root(i) for i in range(len(rows))]


def paired_interval(values, groups):
    """Resample connected groups, keeping each recording's paired difference."""
    values = np.asarray(values, dtype=float)
    unique = sorted(set(groups))
    if not len(values):
        return {'n': 0, 'groups': 0, 'mean': None, 'ci95': None}
    result = {'n': len(values), 'groups': len(unique), 'mean': float(values.mean()), 'ci95': None}
    if len(unique) < 2:
        return result
    groups = np.asarray(groups)
    sums = np.asarray([values[groups == key].sum() for key in unique])
    counts = np.asarray([(groups == key).sum() for key in unique])
    draws = np.random.default_rng(SEED).integers(0, len(unique), size=(2000, len(unique)))
    means = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
    result['ci95'] = np.quantile(means, [.025, .975]).tolist()
    return result


def positive_metrics(rows, profiles, vibes):
    result = {}
    for vibe in vibes:
        # A missing selection is unknown, never an invented negative target.
        selected = [r for r in rows if vibe in r['labels'] and
                    all(r['key'] in profiles[a] for a in ARMS)]
        groups = connected_groups(selected)
        errors = {arm: [1 - profiles[arm][r['key']]['vibes']['fits'][vibe]['score']
                        for r in selected] for arm in ARMS}
        result[vibe] = {'positive_recordings': len(selected), 'groups': len(set(groups)),
            'positive_only_mae': {a: float(np.mean(v)) if v else None for a, v in errors.items()},
            'error_difference_vs_metadata': {a: paired_interval(
                np.asarray(errors[a]) - np.asarray(errors['metadata']), groups) for a in ('signal', 'combined')}}
    return result


def verify_receipt(directory):
    receipt = json.loads((directory / 'receipt.json').read_text())
    for filename, expected in receipt['outputs'].items():
        if digest(directory / filename) != expected:
            raise ValueError('Changed retained output: ' + str(directory / filename))
    return receipt


def output_difference(left, right):
    a, b = [t['key'] for t in left], [t['key'] for t in right]
    common, union = set(a) & set(b), set(a) | set(b)
    return {'left_tracks': len(a), 'right_tracks': len(b), 'common_recordings': len(common),
        'jaccard': len(common) / len(union) if union else None,
        'identical_order': a == b,
        'same_positions': sum(x == y for x, y in zip(a, b)),
        'interpretation': 'Selection difference, not a quality judgment.'}


def surface_difference(left, right):
    lm, lr = left
    rm, rr = right
    return {'mixes': {v: output_difference([t for c in lm[v] for t in c['tracks']],
                                          [t for c in rm[v] for t in c['tracks']]) for v in lm},
            'radio': {dial: output_difference(lr[dial], rr[dial]) for dial in lr}}


def verify_served_profiles(tracks, expected, *, unknown=False):
    for track in tracks:
        profile = track.get('musical') or {}
        if unknown:
            if any(fit['source'] != 'heuristic' or fit['support'] != 0
                   for fit in profile['vibes']['fits'].values()):
                raise ValueError('First incomplete signal capture leaked a measured/inherited fit')
        elif profile != expected.get(track['key']):
            raise ValueError('Consumer served a different profile from its declared arm: ' + track['key'])


def coverage(tracks, labels, vibe=None):
    from yt_playlist.rec import musical_character
    known = [t for t in tracks if t['key'] in labels]
    window_known = [t for t in known if labels[t['key']].get('capture_windows_match', False)]
    intensity = [musical_character.intensity_evidence(t)[0] for t in tracks]
    provenance = [musical_character.provenance(t) for t in tracks]
    measured = [v for v in intensity if v is not None]
    source = Counter()
    if vibe:
        for track in tracks:
            source[(track.get('musical') or {}).get('vibes', {}).get('fits', {}).get(vibe, {}).get('source', 'missing')] += 1
    return {'tracks': len(tracks), 'unique_recordings': len({t['key'] for t in tracks}),
        'unique_artists': len({t.get('artist') for t in tracks if t.get('artist')}),
        'exact_recording_labels': len(known), 'unknown_reference': len(tracks) - len(known),
        'exact_capture_window_labels': len(window_known),
        'selected_vibe_positives': sum(vibe in labels[t['key']]['labels'] for t in known) if vibe else None,
        'selected_vibe_window_positives': sum(vibe in labels[t['key']]['labels'] for t in window_known) if vibe else None,
        'unknown_vibe_reference': len(tracks) - sum(vibe in labels[t['key']]['labels'] for t in known) if vibe else None,
        'vibe_sources': dict(source), 'known_intensity': len(measured),
        'unknown_intensity': len(intensity) - len(measured),
        'intensity_sources': dict(Counter(p['intensity_source'] or
            ('metadata_dimension' if p['intensity'] is not None else 'unknown') for p in provenance)),
        'mean_known_intensity': float(np.mean(measured)) if measured else None,
        'mean_adjacent_known_intensity_delta': float(np.mean([abs(a - b) for a, b in zip(intensity, intensity[1:])
            if a is not None and b is not None])) if any(a is not None and b is not None
                for a, b in zip(intensity, intensity[1:])) else None}


def input_cohort_coverage(tracks, metadata_profiles, partial):
    """Describe retained input sparsity even when an arm ablates those inputs."""
    dimensions = [metadata_profiles[t['key']]['dimensions'] for t in tracks]
    known_counts = [sum(d.get('value') is not None for d in row.values()) for row in dimensions]
    return {'output_slots': len(tracks), 'known_metadata_dimensions': dict(Counter(known_counts)),
            'conflicting_metadata_slots': sum(any(d.get('conflict') for d in row.values()) for row in dimensions),
            'originally_incomplete_capture_slots': sum(t['key'] in partial for t in tracks)}


def external_inventory():
    directory = ROOT / 'experiments/audio_lab/local/individual-20261030/remaining-1/inputs'
    result = {}
    for path in sorted(directory.glob('*.json')):
        data = json.loads(path.read_text())
        rows = data.get('rows') if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict) or 'id' not in rows[0]:
            continue
        result[path.stem] = {'path': str(path), 'sha256': digest(path), 'rows': len(rows),
            'annotation_units': dict(Counter(str(r.get('annotation_unit')) for r in rows)),
            'explicit_video_identity_rows': sum(bool(r.get('video_id')) for r in rows),
            'verified_local_recording_version_window_joins': 0,
            'native_and_fixed_mapped_output_metrics': None,
            'limit': 'No verified local recording/version/window crosswalk; names alone cannot transfer these labels.'}
    return result


def report(directory, local, runs):
    from yt_playlist.rec import home_vibe
    verify_receipt(local)
    annotations = json.loads((local / 'labels.json').read_text())
    arms, profiles = {}, {}
    for run in runs:
        receipt = verify_receipt(run)
        data = json.loads((run / 'report.json').read_text())
        if receipt['plan_sha256'] != digest(directory / 'plan.json') or not data['complete']:
            raise ValueError('Incomplete or mismatched arm')
        arm = data['arm']
        if arm in arms:
            raise ValueError('Ambiguous repeated arm: specify one retained run explicitly')
        arms[arm] = (run, data)
        profiles[arm] = json.loads((run / 'profiles.json').read_text())
    if set(arms) != set(ARMS):
        raise ValueError('All three declared arms are required')
    metadata_report = arms['metadata'][1]
    metadata_restrictions = json.loads((arms['metadata'][0] / 'restrictions.json').read_text())
    for run, data in arms.values():
        if (data['seed_sha256'] != metadata_report['seed_sha256'] or
                data['explicit_genres'] != metadata_report['explicit_genres'] or
                data['inventory'] != metadata_report['inventory'] or
                data['remaining_preparation'] != metadata_report['remaining_preparation']):
            raise ValueError('Paired arms changed their seed, restrictions or input inventory')
        restrictions = json.loads((run / 'restrictions.json').read_text())
        if set(restrictions) != set(metadata_restrictions) or any(
                value.get('excluded') != metadata_restrictions[name].get('excluded')
                for name, value in restrictions.items()):
            raise ValueError('Paired arms changed explicit restriction cases')
    with closing(sqlite3.connect((directory / 'source.db').as_uri() + '?mode=ro', uri=True)) as conn:
        identities = dict(conn.execute('SELECT y.video_id,r.identity_key FROM youtube_items y JOIN recordings r ON r.id=y.recording_id'))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        artist_table = 'entry_artists' if 'entry_artists' in tables else 'track_artists'
        artist_ids = {}
        for key, artist in conn.execute('SELECT DISTINCT r.identity_key,a.artist_id FROM recordings r '
                'JOIN catalog_entries e ON e.recording_id=r.id JOIN ' + artist_table + ' a ON a.entry_id=e.id'):
            artist_ids.setdefault(key, []).append(artist)
        # An album name is not a globally unique album identity. Qualify it by
        # canonical artist set; missing names cannot connect unrelated tracks.
        albums = {r[0]: r[1:] for r in conn.execute('SELECT video_id,album,album_browse_id FROM youtube_items')}
    captures = json.loads((arms['combined'][0] / 'capture-joins.json').read_text())
    joined, missing = [], []
    for row in annotations['rows']:
        key = identities.get(row['video_id'])
        if key is None:
            missing.append(row['video_id'])
            continue
        artists = sorted(artist_ids.get(key, []))
        if not artists and row['artist'].strip():
            artists = ['name:' + row['artist'].strip().casefold()]
        album_name, album_id = albums.get(row['video_id'], (None, None))
        album = (album_name or '').strip().casefold()
        joined.append({**row, 'key': key, 'artists': artists,
            'albums': [album_id] if album_id else [json.dumps([artists, album])] if artists and album else [],
            'capture_windows_match': sorted(row['sample_ids']) == sorted(captures.get(key, []))})
    # Multiple videos of one recording must not inflate the statistical unit.
    unique = {}
    for row in joined:
        old = unique.get(row['key'])
        if old is None or row['updated_at'] > old['updated_at']:
            unique[row['key']] = row
    joined = list(unique.values())
    labels = {row['key']: row for row in joined}
    result = {'plan_sha256': digest(directory / 'plan.json'), 'local_receipt_sha256': digest(local / 'receipt.json'),
        'local': {'usable_export_rows': len(annotations['rows']), 'skipped': annotations['skipped'],
            'exact_recording_joins': len(joined), 'missing_recording_joins': missing,
            'exact_inference_window_joins': sum(r['capture_windows_match'] for r in joined),
            'previously_inspected': sum(r['prior_inspected'] for r in joined),
            'unselected_vibes': 'unknown', 'negative_targets': 0,
            'binary_ranking_precision_unsuitable_inclusions': None,
            'limit': 'Previously inspected track selections; not a holdout, calibrated probability or whole-list judgment.'},
        'external': external_inventory(), 'arms': {},
        'inference_audit': inference_audit(directory, arms['combined'][0], profiles['combined']),
        'positive_metrics_exact_windows': positive_metrics([r for r in joined if r['capture_windows_match']], profiles, home_vibe.PRESETS),
        'positive_metrics_recording_only': positive_metrics(joined, profiles, home_vibe.PRESETS)}
    complete_outputs, partial_outputs = {}, {}
    for arm, (run, data) in arms.items():
        surfaces = {}
        for phase in data['phases']:
            mixes = json.loads((run / (phase + '-mixes.json')).read_text())
            radio = json.loads((run / (phase + '-radio.json')).read_text())
            expected = profiles['metadata'] if phase == 'first-excerpts' else profiles[arm]
            tracks = [t for cards in mixes.values() for card in cards for t in card['tracks']]
            tracks += [t for episode in radio.values() for t in episode]
            verify_served_profiles(tracks, expected, unknown=phase == 'first-excerpts' and arm == 'signal')
            if phase == 'complete':
                complete_outputs[arm] = (mixes, radio)
            else:
                partial_outputs[arm] = (mixes, radio)
            surfaces[phase] = {'mixes': {v: [coverage(c['tracks'], labels, v) for c in cards]
                                          for v, cards in mixes.items()},
                               'radio': {dial: coverage(tracks, labels) for dial, tracks in radio.items()},
                               'input_cohorts': input_cohort_coverage(tracks, profiles['metadata'],
                                   set(metadata_report['inventory']['missing_or_partial_captures']))}
        result['arms'][arm] = {'path': str(run), 'report': data, 'surfaces': surfaces}
    result['paired_output_differences'] = {}
    for left, right in (('metadata', 'signal'), ('metadata', 'combined'), ('signal', 'combined')):
        result['paired_output_differences'][left + '_vs_' + right] = surface_difference(
            complete_outputs[left], complete_outputs[right])
    result['first_excerpt_differences_vs_metadata'] = {arm: surface_difference(
        complete_outputs['metadata'], outputs) for arm, outputs in partial_outputs.items()}
    save(directory / 'comparison.json', result)
    if replay_runtime.runtime_hashes() != json.loads((directory / 'plan.json').read_text())['runtime_files']:
        raise ValueError('Report runtime changed')
    save(directory / 'comparison-receipt.json', {'report_sha256': digest(directory / 'comparison.json'),
        'script_sha256': digest(__file__), 'local_receipt_sha256': digest(local / 'receipt.json'),
        'arm_receipts': {arm: digest(run / 'receipt.json') for arm, (run, _) in arms.items()}})
    print(json.dumps({'comparison': str(directory / 'comparison.json'), 'local': result['local']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--local', type=Path, required=True)
    parser.add_argument('--run', type=Path, action='append', required=True)
    args = parser.parse_args()
    activate_runtime(inside(args.directory))
    report(inside(args.directory), inside(args.local), [inside(p) for p in args.run])


if __name__ == '__main__':
    main()
