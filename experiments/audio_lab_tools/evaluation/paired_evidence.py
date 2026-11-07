"""Paired uncertainty for fixed procedures on historical out-of-fold songs.

No candidate is fitted or selected here. The reserved additions are not read.
"""
import argparse
import json

import numpy as np
from threadpoolctl import threadpool_limits

from ..studies.completion_campaign import VARIANTS, stage_outputs
from ..training.external_binary_calibration import metrics
from ..studies.external_checkpoint import CORE, RUN
from ..support.external_experiment import OUT, digest, normalize, save, stamp
from .external_ranking import average_precision
from ..training.external_training import SEED
from .external_transfer import SNAPSHOT, VIBES
from ..studies.heldout_cohort import paired_bootstrap


def ranking_interval(y, baseline, candidate, groups):
    """Resample complete artist groups, retaining a fixed set of scored vibes."""
    labels = y > 0
    columns = [column for column in range(y.shape[1]) if labels[:, column].any()]
    keys = sorted(set(groups))
    members = [np.flatnonzero(np.asarray(groups) == key) for key in keys]
    draws = np.random.default_rng(SEED).integers(len(keys), size=(2000, len(keys)))
    values = []
    for draw in draws:
        selected = np.concatenate([members[index] for index in draw])
        if not labels[selected][:, columns].any(axis=0).all():
            continue
        values.append(np.mean([average_precision(labels[selected, column], candidate[selected, column]) -
                               average_precision(labels[selected, column], baseline[selected, column])
                               for column in columns]))
    difference = np.mean([average_precision(labels[:, column], candidate[:, column]) -
                          average_precision(labels[:, column], baseline[:, column]) for column in columns])
    return {'macro_average_precision_gain': float(difference),
        'conditional_95_percent_interval': np.quantile(values, [.025, .975]).tolist(),
        'usable_bootstrap_draws': len(values), 'total_draws': len(draws),
        'vibes': [VIBES[column] for column in columns],
        'policy': 'Paired artist-group bootstrap of fixed out-of-fold predictions. Draws without a positive for an originally scored vibe are excluded; model-selection uncertainty is not repeated.'}


def evaluate():
    snapshot = json.loads(SNAPSHOT.read_text())
    rows = [row for row in snapshot['gold'] if len(row.get('embedding_values', [])) == 200]
    ids = [row['video_id'] for row in rows]
    y = np.asarray([[int(vibe in row['labels']) for vibe in VIBES] for row in rows])
    groups = [normalize(row['artist']) or row['video_id'] for row in rows]
    core = 'checkpoint530__' + '+'.join(CORE)
    sources = {'core_consensus': OUT / f'consensus-{core}.oof.json',
        'core_selected_learners': OUT / f'consensus-{core}__native-selected_learner__calibrator-selected_learner.oof.json',
        'core_rankings': OUT / f'vibe-ranking-{core}.oof.json'}
    sources.update({'consensus_' + name: stage_outputs('consensus:' + name)[1]
                    for name in (*VARIANTS, 'expanded')})
    sources['expanded_rankings'] = stage_outputs('ranking:expanded')[1]
    report = {'created_at': stamp(), 'script_sha256': digest(__file__), 'snapshot_sha256': digest(SNAPSHOT),
        'rows': len(rows), 'groups': len(set(groups)), 'input_sha256': {}, 'methods': {},
        'limitations': ['Historical developer labels have been repeatedly examined; these are exploratory comparisons.',
            'Artist string folds do not resolve every collaboration or alias. Upstream training overlap remains unknown.',
            'Intervals condition on fitted out-of-fold predictions and exclude model-selection uncertainty.',
            'No correction for the many reported comparisons; small apparent differences do not establish a winner.',
            'The old reported product checkpoint scores use different folds. Its unchanged full artifact is compared only on the separately frozen reserved cohort.']}
    baseline, reference_folds = None, None
    heuristic = np.asarray([[row['baseline'][vibe] for vibe in VIBES] for row in rows])
    for name, path in sources.items():
        report['input_sha256'][str(path)] = digest(path)
        data = json.loads(path.read_text())
        observed_ids = data.get('ids') or [row['video_id'] for row in data['rows']]
        split = data.get('folds') or [row['fold'] for row in data['rows']]
        if observed_ids != ids or not np.array_equal(np.asarray(data['observed']) > 0, y > 0):
            raise ValueError('Paired historical cohort differs: ' + name)
        if reference_folds is None:
            reference_folds = split
            baseline = np.asarray(data['predictions']['expert:embedding'])
        if split != reference_folds:
            raise ValueError('Paired historical outer folds differ: ' + name)
        retained = {key: np.asarray(value) for key, value in data['predictions'].items()
                    if key.startswith('expert:') or ':' not in key}
        if name == 'core_consensus':
            retained['captured_heuristic'] = heuristic
        methods = {}
        for method, values in retained.items():
            methods[method] = {'metrics': metrics(y, values),
                'versus_core_embedding': paired_bootstrap(y, baseline, values, groups)}
            if method in ('selected_candidate', 'selected_consensus', 'selected_combination', 'equal_all', 'weighted_all'):
                methods[method]['ranking_versus_core_embedding'] = ranking_interval(y, baseline, values, groups)
        report['methods'][name] = methods
        save(RUN / 'historical-paired.report.json', report)
        print(json.dumps({'historical_paired': name, 'procedures': len(methods),
            'selected': {key: value for key, value in methods.items() if key == 'selected_candidate'}}), flush=True)
    save(RUN / 'historical-paired.report.json', report)
    return report


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    with threadpool_limits(limits=1):
        evaluate()


if __name__ == '__main__':
    main()
