"""Match full-excerpt emotion supervision to multiple separated audio windows."""
import copy
import hashlib
import json
from pathlib import Path

import numpy as np

from .external_experiment import OUT, digest, extract, save, stamp


def aggregate(dataset):
    manifests = [OUT / f'{name}.features.json' for name in
                 (dataset + '-first', dataset, dataset + '-last')]
    by_position = [{row['id']: row for row in json.loads(path.read_text())['rows']}
                   for path in manifests]
    shared = set.intersection(*(set(rows) for rows in by_position))
    if len(shared) != len(by_position[1]):
        raise ValueError('Multiwindow comparison requires all center recordings at every position')
    rows = []
    for identifier in by_position[1]:
        views = [position[identifier] for position in by_position]
        if len({row['audio_sha256'] for row in views}) != 1:
            raise ValueError('Different recordings share a multiwindow identifier')
        if len({row['extractor_signature'] for row in views}) != 1:
            raise ValueError('Multiple audio windows have incompatible extractor provenance')
        if any(row['targets'] != views[1]['targets'] or row['group'] != views[1]['group'] for row in views):
            raise ValueError('Multiwindow annotations or validation groups changed')
        windows = [{'start_seconds': row['sample_start'], 'seconds': row['sample_seconds'],
                    'feature_file': row['feature_file'], 'feature_sha256': digest(row['feature_file'])}
                   for row in views]
        result = copy.deepcopy(json.loads(Path(views[1]['feature_file']).read_text()))
        originals = [json.loads(Path(row['feature_file']).read_text()) for row in views]
        result['id'] = identifier
        result['research_aggregation'] = {'policy': 'equal_mean_first_center_last_ten_seconds',
                                           'windows': windows}
        result['descriptors'] = {key: float(np.mean([value['descriptors'][key] for value in originals]))
                                  for key in result['descriptors']}
        attributes = result['audio_attributes']
        attributes['values'] = {key: float(np.mean([value['audio_attributes']['values'][key]
                                                   for value in originals])) for key in attributes['values']}
        attributes.update(seconds=30, scope='three_separated_ten_second_windows')
        prediction = result['prediction']
        prediction['embedding']['values'] = np.mean([value['prediction']['embedding']['values']
                                                      for value in originals], axis=0).tolist()
        for key, trait in prediction['moods']['traits'].items():
            trait['score'] = float(np.mean([value['prediction']['moods']['traits'][key]['score']
                                           for value in originals]))
            trait['per_patch'] = [score for value in originals
                                  for score in value['prediction']['moods']['traits'][key]['per_patch']]
        prediction['scores'] = {key: float(np.mean([value['prediction']['scores'][key]
                                                   for value in originals])) for key in prediction['scores']}
        prediction['patches'] = sum(value['prediction']['patches'] for value in originals)
        prediction['analysed_seconds'] = sum(value['prediction']['analysed_seconds'] for value in originals)
        result['sample_count'] = sum(value['sample_count'] for value in originals)
        for key in ('inference_ms', 'startup_ms'):
            prediction[key] = sum(value['prediction'][key] for value in originals)
        result['wall_ms'] = sum(value['wall_ms'] for value in originals)
        cache_key = hashlib.sha256(json.dumps(windows, sort_keys=True).encode()).hexdigest()
        feature_path = OUT / 'features-multiwindow' / (cache_key + '.json')
        save(feature_path, result)
        rows.append({**views[1], 'feature_file': str(feature_path), 'sample_start': None,
                     'sample_seconds': 30, 'sample_windows': windows,
                     'aggregation_policy': 'equal_mean_first_center_last_ten_seconds'})
    alias = dataset + '-multiwindow'
    save(OUT / f'{alias}.features.json', {'rows': rows, 'errors': [], 'created_at': stamp(),
         'provenance': {'source_features_sha256': {path.name: digest(path) for path in manifests},
                        'script_sha256': digest(__file__), 'aggregation': 'Equal feature means over three separated ten-second excerpts; original full-excerpt annotations and recording groups unchanged.'}})
    return alias


def evaluate(dataset='emotify'):
    from .external_training import evaluate as native
    from .external_learners import evaluate as learners
    from .external_transfer import evaluate_transfer
    from .external_vibe_ranking import evaluate as ranking
    # The sequential follow-up runner calls this only after other extraction.
    # Stay inside the decoded signal: compressed container duration can include
    # encoder padding, and the final reported ten seconds can decode short.
    extract(dataset, native=True, sample_fraction=.05, output_dataset=dataset + '-first')
    extract(dataset, native=True, sample_fraction=.95, output_dataset=dataset + '-last')
    alias = aggregate(dataset)
    report = native(alias)
    learners(alias)
    evaluate_transfer([alias])
    ranking([alias, 'pmemo'])
    save(OUT / f'{dataset}.multiwindow-comparison.json', {'created_at': stamp(),
         'dataset': dataset, 'multiwindow_dataset': alias, 'rows': report['rows'],
         'center_report_sha256': digest(OUT / f'{dataset}.report.json'),
         'multiwindow_report_sha256': digest(OUT / f'{alias}.report.json'),
         'comparison': 'Original full-excerpt labels, recordings and grouped folds held fixed; compare center-only features with averaged first/center/last features.',
         'limitations': ['Multiple excerpts improve coverage without establishing that every section has the same mood.',
                        'Averaging short-window attributes does not reproduce attributes computed from a continuous thirty-second waveform.',
                        'Emotify lacks artist identities; native validation separates recordings only.',
                        'The same small developer gold remains exploratory.']})
    return report
