"""Build separate blind-listening and evidence artifacts from local lab exports.

uv run python -m experiments.audio_lab_tools.evaluation --output /tmp/audio-evaluation export.json ...
Never opens the live database, downloads recordings or manufactures judgments.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import statistics

from tests.musical_evaluation_support import holdouts
from yt_playlist.core.metadata_observations import MetadataObservation
from yt_playlist.rec.home_vibe import PRESETS


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate(report):
    reference = report.get('reference') or {}
    source = reference.get('source')
    if report.get('schema') != 1 or not re.fullmatch(r'sha256:[a-f0-9]{64}', source or ''):
        raise ValueError('Unknown report schema or file identity')
    if not reference.get('permission_note', '').strip():
        raise ValueError('Source permission is required')
    if report.get('recommendation_adoption') is not False:
        raise ValueError('Experimental reports cannot assert recommendation adoption')
    seen = set()
    for row in report.get('results', []):
        if row.get('source') != source or row.get('id') in seen:
            raise ValueError('Mismatched source or duplicate sample delivery')
        seen.add(row.get('id'))
        start, end, duration, rate, count = (row.get(name) for name in (
            'start', 'end', 'duration', 'sample_rate', 'sample_count'))
        if not all(_finite(v) for v in (start, end, duration, rate, count)) or not (
                0 <= start < end <= duration and end - start <= 10.025
                and 8000 <= rate <= 192000 and abs(count / rate - (end - start)) <= .025):
            raise ValueError('Invalid or unbounded analysed interval')
        if row.get('raw_audio_retained') is not False or row.get('recording_association') is not None:
            raise ValueError('Unexpected raw audio or asserted recording association')
        if row.get('intensity') is not None:
            raise ValueError('Unvalidated signal measurements cannot assert intensity')
        if not row.get('policy') or not row.get('derivation_version'):
            raise ValueError('Sampling and derivation versions are required')
        for value in row.get('descriptors', {}).values():
            if value is not None and not _finite(value):
                raise ValueError('Nonfinite descriptor')
        prediction = row.get('prediction')
        if prediction and (prediction.get('kind') != 'prediction' or prediction.get('reliability') is not None):
            raise ValueError('Model scores cannot assert calibrated reliability')
    return report


def observations(report):
    """Convert to existing evidence objects only; callers own any scratch ingestion.

    Local file bytes are the subject. No catalog/recording association is inferred,
    and native audio fields are not selected musical-profile dimensions.
    """
    validate(report)
    native_units = {'rms': 'linear_full_scale', 'peak': 'linear_full_scale', 'rms_dbfs': 'dBFS',
                    'crest_factor': 'ratio', 'zero_crossings_per_second': 's^-1', 'clipped_fraction': 'fraction'}
    result = []
    for row in report.get('results', []):
        evidence = {'source_identity': row['source'], 'recording_association': None,
                    'interval_s': [row['start'], row['end']], 'duration_s': row['duration'],
                    'sample_coverage': (row['end'] - row['start']) / row['duration'],
                    'sampling_policy': row['policy'], 'reference_only': True,
                    'interval_basis': row.get('interval_basis', 'unknown'),
                    'clock_tolerance_s': row.get('clock_tolerance_s'),
                    'permission_note': report['reference']['permission_note']}
        for field, value in row['descriptors'].items():
            if value is None:
                continue
            if field not in native_units:
                raise ValueError('Unknown signal descriptor')
            result.append(MetadataObservation(field='audio.' + field, value=value, scope='item',
                entity_id=row['source'], kind='measurement', unit=native_units[field],
                source_path='reference_audio/' + row['id'], derivation_version=row['derivation_version'],
                match_evidence=evidence))
        prediction = row.get('prediction') or {}
        for label, score in (prediction.get('scores') or {}).items():
            if not isinstance(label, str) or not _finite(score) or not 0 <= score <= 1:
                raise ValueError('Invalid native tag prediction')
            seconds = prediction.get('analysed_seconds')
            if not _finite(seconds) or not 0 < seconds <= row['end'] - row['start']:
                raise ValueError('Invalid model coverage')
            result.append(MetadataObservation(field='audio.native_tag.' + label, value=score, scope='item',
                entity_id=row['source'], kind='prediction', unit='classifier_score',
                source_path='reference_audio/' + row['id'],
                model_version={name: prediction.get(name) for name in ('model', 'model_version', 'runtime_version', 'bundle')},
                derivation_version=prediction.get('feature_policy'),
                match_evidence={**evidence, 'capture_interval_s': evidence['interval_s'],
                                'interval_s': [row['start'], row['start'] + seconds],
                                'sample_coverage': seconds / row['duration'],
                                'reliability': None}))
    return result


def packet(reports):
    """#405-style blind fit/pair judgments, separated from native predictions."""
    reports = [validate(report) for report in reports]
    unique = {}
    for report in reports:
        reference = report['reference']
        key = reference['source']
        if key in unique:
            raise ValueError('Use one export per reference file in a comparison packet')
        unique[key] = report
    music = [report for report in reports if report['reference'].get('synthetic') is False]
    rows = [{'key': report['reference']['source'], **{name: report['reference'].get(name, '')
             for name in ('title', 'artist', 'album')}} for report in music]
    splits = holdouts(rows)
    blind, manifest, pairs = [], [], []
    for row in rows:
        key = row['key']
        for preset in PRESETS:
            identity = _digest([key, preset])[:20]
            blind.append({'id': identity, 'preset': preset, 'title': row['title'], 'artist': row['artist'],
                          'file': unique[key]['reference'].get('filename'), 'fit': None,
                          'intrusion': None, 'notes': ''})
            manifest.append({'id': identity, 'key': key, **splits[key]})
    # Fixed blind ordering, with connected artist/album groups in the same split.
    blind.sort(key=lambda row: _digest([row['id'], 'audio-blind-order-1']))
    ordered = sorted(rows, key=lambda row: _digest([row['key'], 'audio-pairs-1']))
    for index in range(0, len(ordered) - 1, 2):
        def credit(row):
            return {name: row[name] for name in ('key', 'title', 'artist')}
        pairs.append({'id': 'audio-intensity-' + str(index // 2), 'left': credit(ordered[index]),
                      'right': credit(ordered[index + 1]), 'more_intense': None, 'notes': ''})
    listener = {'schema': 1, 'status': 'unlabelled', 'fit_values': ['fits', 'does_not_fit', 'unsure'],
                'pair_values': ['left', 'right', 'similar', 'unsure'], 'tracks': blind, 'intensity_pairs': pairs,
                'instructions': 'Listen before judging. Leave uncertain or unheard examples unlabelled.'}
    costs = [row['prediction']['inference_ms'] for report in reports for row in report.get('results', [])
             if row.get('prediction') and _finite(row['prediction'].get('inference_ms'))]
    report = {'schema': 1, 'reference_music_files': len(music),
              'synthetic_files_excluded_from_listening': len(reports) - len(music),
              'reference_target': [20, 30], 'human_judgments': 0,
              'identity_review': sum(s['split'] == 'identity_review' for s in splits.values()),
              'inference_wall_ms': {'count': len(costs), 'median': statistics.median(costs) if costs else None,
                  'max': max(costs) if costs else None},
              'production_decision': 'no_go_pending_evidence',
              'reasons': ['Human judgments and a permitted 20–30 track reference corpus are required.',
                          'Shared-trait mappings and metadata-versus-audio listening benefit are unvalidated.',
                          'Native scores, coverage and synthetic measurements are not calibrated reliability.',
                          'Source feasibility, resource budgets and artifact licences require review.']}
    return listener, {'schema': 1, 'manifest': manifest, 'sources': reports}, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('exports', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    listener, evidence, report = packet([json.loads(path.read_text()) for path in args.exports])
    args.output.mkdir(parents=True, exist_ok=False)
    for name, value in [('listener.json', listener), ('evidence.json', evidence), ('report.json', report)]:
        path = args.output / name
        path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
        path.chmod(0o600)
    print(json.dumps({'output': str(args.output), **report}))


if __name__ == '__main__':
    main()
