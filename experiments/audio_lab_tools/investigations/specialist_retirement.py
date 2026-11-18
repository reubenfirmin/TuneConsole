"""Inspect specialist-only inference against an immutable captured library.

Run through scripts/test-library.py run --readonly. This measures compatibility
and score coverage, not enjoyment prediction or model calibration.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
from statistics import median

from yt_playlist.core import sample_files
from yt_playlist.library import vibe_composition
from yt_playlist.rec import audio_vibe_specialists, vibe_intensity


def audit(database, *, limit=1024):
    for _vibe, module in audio_vibe_specialists.SPECIALISTS:
        os.environ[module.ENVIRONMENT_KEY] = str(module.DEFAULT_PATH)
        os.environ[module.MODE_KEY] = 'active'
    captured = vibe_composition.prediction().capture()
    values = {vibe: [] for vibe, _module in audio_vibe_specialists.SPECIALISTS}
    failures, unavailable, examples = Counter(), Counter(), []
    seen, attempted, complete, intense = set(), 0, 0, []
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(sample_attributes)')}
        ready = "WHERE file_status='ready'" if 'file_status' in columns else ''
        groups = conn.execute('SELECT recording_id,run_id,MAX(captured_at) AS newest '
            f'FROM sample_attributes {ready} GROUP BY recording_id,run_id '
            'HAVING COUNT(DISTINCT point)=3 ORDER BY newest DESC').fetchall()
        for group in groups:
            if group['recording_id'] in seen:
                continue
            seen.add(group['recording_id'])
            if attempted >= limit:
                break
            attempted += 1
            rows = conn.execute('SELECT * FROM sample_attributes WHERE recording_id=? AND run_id=? '
                'ORDER BY captured_at,id', (group['recording_id'], group['run_id'])).fetchall()
            if len(rows) != 3:
                failures['not_three_samples'] += 1
                continue
            try:
                samples = [sample_files.read(row['filename'], expected={name: row[name] for name in
                    ('id', 'run_id', 'point', 'video_id')},
                    digest=row['content_digest'] if 'content_digest' in columns else None) for row in rows]
            except (OSError, ValueError) as error:
                failures[type(error).__name__] += 1
                continue
            result = captured.predict(samples)
            if result is None:
                failures['incomplete_prediction'] += 1
                continue
            assert result['vibe'] is None and not result['vibes'] and not result['label']
            assert result['selection_method'] == 'independent_specialists'
            ordered = sorted(samples, key=lambda sample: (
                sample['position_seconds'], sample['target_seconds'], sample['id']))
            for vibe, score in result['suitability'].items():
                assert 0 <= score <= 1
                individual = result['individual_models'][vibe]
                assert individual['score'] == score
                assert [row['sample_id'] for row in individual['excerpts']] == [row['id'] for row in ordered]
                values[vibe].append(score)
            complete += set(result['suitability']) == set(values)
            unavailable.update(result['unavailable'])
            fits = {vibe: {'score': score, 'support': 1., 'source': 'audio_measurement'}
                    for vibe, score in result['suitability'].items()}
            intensity = vibe_intensity.ensemble(fits)
            if intensity:
                assert intensity['score'] == max(fits[name]['score'] for name in ('energize', 'release') if name in fits)
                intense.append(intensity['score'])
            if len(examples) < 3:
                examples.append({'recording_id': group['recording_id'], 'scores': result['suitability'],
                                 'unavailable': result['unavailable'], 'intensity': intensity})
    distributions = {vibe: {'count': len(scores), 'minimum': min(scores),
                           'median': median(scores), 'maximum': max(scores)}
                     for vibe, scores in values.items() if scores}
    return {'database': str(database), 'recording_limit': limit, 'attempted_recordings': attempted,
        'all_six_available': complete, 'failures': dict(failures),
        'unavailable': dict(unavailable), 'scores': distributions,
        'intensity': {'count': len(intense), 'median': median(intense) if intense else None},
        'artifacts': {vibe: module.identity() for vibe, module in audio_vibe_specialists.SPECIALISTS},
        'examples': examples,
        'scope': 'Read-only captured excerpts; compatibility and coverage, not taste or calibration.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=1024)
    args = parser.parse_args()
    result = audit(args.database, limit=args.limit)
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: value for key, value in result.items() if key not in ('examples', 'artifacts')}))
    if not result['all_six_available']:
        raise SystemExit('No complete specialist prediction observed')


if __name__ == '__main__':
    main()
