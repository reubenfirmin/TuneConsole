"""Check Emotify emotion prediction against a genre-only baseline."""
import argparse
import csv
import json
import time

import numpy as np

from .external_experiment import DATA, OUT, digest, save, stamp, table
from .external_training import metrics


def evaluate():
    source = OUT / 'emotify.oof.csv'
    rows = json.loads((OUT / 'emotify.features.json').read_text())['rows']
    target_names = rows[0]['target_names']
    ids = {row['id']: i for i, row in enumerate(rows)}
    columns = {name: i for i, name in enumerate(target_names)}
    identities = {f"emotify:{row['track id']}": row['genre'] for row in table(DATA / 'emotify/data.csv')}
    genres = np.array([identities[row['id']] for row in rows])
    observed = np.array([row['targets'] for row in rows])
    split = np.full(len(rows), -1)
    with source.open(newline='') as stream:
        reader = csv.DictReader(stream)
        families = [key for key in reader.fieldnames if key not in ('id', 'group', 'fold', 'target', 'observed')]
        predictions = {key: np.full_like(observed, np.nan) for key in families}
        for value in reader:
            i, j = ids[value['id']], columns[value['target']]
            split[i] = int(value['fold'])
            for key in families:
                predictions[key][i, j] = float(value[key])
    if min(split) < 0 or not all(np.isfinite(matrix).all() for matrix in predictions.values()):
        raise ValueError('Incomplete held-out predictions')
    genre_baseline = np.zeros_like(observed)
    for fold in sorted(set(split)):
        for genre in sorted(set(genres)):
            train = (split != fold) & (genres == genre)
            test = (split == fold) & (genres == genre)
            if not train.any():
                raise ValueError('No genre baseline training examples')
            genre_baseline[test] = observed[train].mean(axis=0)
    predictions['training_genre_mean'] = genre_baseline
    results = {key: metrics(observed, values, target_names) for key, values in predictions.items()}
    per_genre = {genre: {key: metrics(observed[genres == genre], values[genres == genre], target_names)
                         for key, values in predictions.items()} for genre in sorted(set(genres))}
    report = {'created_at': stamp(), 'dataset': 'emotify', 'source_oof_sha256': digest(source),
              'rows': len(rows), 'genres': {genre: int(sum(genres == genre)) for genre in sorted(set(genres))},
              'overall': results, 'within_genre': per_genre,
              'limitations': ['Genre baseline uses training folds only; saved model predictions are unchanged.',
                              'Within-genre results reuse recording-held-out folds; artist identities are unavailable.',
                              'This checks a genre shortcut within four supplied genres, not generalization to unseen genres.']}
    save(OUT / 'emotify.genre-check.json', report)
    lines = ['# Emotify genre check', '',
             '| Features | Overall MAE | Classical Pearson | Electronic Pearson | Pop Pearson | Rock Pearson |',
             '| --- | ---: | ---: | ---: | ---: | ---: |']
    for key, value in results.items():
        def number(value):
            return 'undefined' if value is None else f'{value:.3f}'
        lines.append(f"| {key} | {value['mae']:.4f} | " + ' | '.join(
            number(per_genre[genre][key]['pearson']) for genre in ['classical', 'electronic', 'pop', 'rock']) + ' |')
    lines.extend(['', *['- ' + value for value in report['limitations']], ''])
    (OUT / 'emotify.genre-check.md').write_text('\n'.join(lines))
    print(json.dumps({'genre_check': {key: value['mae'] for key, value in results.items()}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wait', action='store_true')
    args = parser.parse_args()
    while args.wait and not (OUT / 'emotify.report.json').exists():
        suite = json.loads((OUT / 'suite-status.json').read_text())
        if suite['state'].startswith('complete'):
            raise RuntimeError('Native suite ended without an Emotify report')
        time.sleep(15)
    evaluate()


if __name__ == '__main__':
    main()
