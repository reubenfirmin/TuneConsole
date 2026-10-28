"""CAL500 ranking diagnostics using saved held-out predictions only."""
import csv
import json

import numpy as np
from scipy.stats import rankdata

from .external_experiment import DATA, OUT, digest, save, stamp


def average_precision(labels, scores):
    if not labels.sum():
        return None
    order = np.argsort(-scores, kind='stable')
    truth, values = labels[order], scores[order]
    ends = np.r_[np.flatnonzero(np.diff(values)), len(values) - 1]
    hits = np.cumsum(truth)[ends]
    return float(np.sum(np.diff(np.r_[0, hits]) / labels.sum() * hits / (ends + 1)))


def auc(labels, scores):
    positives, negatives = int(labels.sum()), int((~labels).sum())
    if not positives or not negatives:
        return None
    ranks = rankdata(scores)
    return float((ranks[labels].sum() - positives * (positives + 1) / 2) / (positives * negatives))


def top_mean(observed, scores, count=10):
    count = min(count, len(scores))
    threshold = np.sort(scores)[-count]
    better, tied = scores > threshold, scores == threshold
    return float((observed[better].sum() + (count - sum(better)) * observed[tied].mean()) / count)


def main():
    source = OUT / 'cal500.oof.csv'
    manifest = json.loads((OUT / 'cal500.features.json').read_text())
    rows = manifest['rows']
    ids = {row['id']: i for i, row in enumerate(rows)}
    names = rows[0]['target_names']
    target_index = {name: i for i, name in enumerate(names)}
    observed = np.array([row['targets'] for row in rows])
    assignments = np.full(len(rows), -1, dtype=int)
    with source.open(newline='') as stream:
        reader = csv.DictReader(stream)
        families = [name for name in reader.fieldnames if name not in ['id', 'group', 'fold', 'target', 'observed']]
        predictions = {name: np.full_like(observed, np.nan) for name in families}
        for value in reader:
            i, j = ids[value['id']], target_index[value['target']]
            assignments[i] = int(value['fold'])
            assert abs(float(value['observed']) - observed[i, j]) < 1e-10
            for family in families:
                predictions[family][i, j] = float(value[family])
    assert all(np.isfinite(matrix).all() for matrix in predictions.values())
    annotations = DATA / 'cal500/annotations-source'
    source_ids = {name: i for i, name in enumerate((annotations / 'songNames.txt').read_text().splitlines())}
    published_binary = np.loadtxt(annotations / 'hardAnnotations.txt', delimiter=',')
    binary = published_binary[[source_ids[row['id'].removeprefix('cal500:')] for row in rows]].astype(bool)
    activity = [j for j, name in enumerate(names) if name.startswith('Usage-')]
    report = {'created_at': stamp(), 'dataset': 'cal500', 'rows': len(rows),
              'oof_sha256': digest(source), 'published_binary_annotation_sha256': digest(annotations / 'hardAnnotations.txt'),
              'limitations': ['Published binary annotations are used as supplied; they are not assumed to require majority consensus.',
                              'Published activity soft weights discard single-Yes choices; their top-ten lift concerns these filtered weights.',
                              'AP and AUC concern native tags, not the six vibe labels.',
                              'Top-ten vote fractions average rankings within each held-out fold, with expected values for tied scores.',
                              'Rare positives produce uncertain estimates; no statistical significance is claimed.'], 'activities': {}}
    for j in activity:
        item = {'positives': int(binary[:, j].sum()), 'prevalence': float(binary[:, j].mean()),
                'mean_vote_fraction': float(observed[:, j].mean()), 'families': {}}
        for family, values in predictions.items():
            top, weights = [], []
            for fold in sorted(set(assignments)):
                test = assignments == fold
                top.append(top_mean(observed[test, j], values[test, j]))
                weights.append(sum(test))
            average_vote = float(np.average(top, weights=weights))
            item['families'][family] = {'average_precision': average_precision(binary[:, j], values[:, j]),
                 'roc_auc': auc(binary[:, j], values[:, j]), 'top_ten_mean_vote_fraction': average_vote,
                 'top_ten_vote_lift_vs_random': average_vote / item['mean_vote_fraction'] if item['mean_vote_fraction'] else None}
        report['activities'][names[j]] = item
    report['macro_activity_average_precision'] = {family: float(np.mean([
        item['families'][family]['average_precision'] for item in report['activities'].values()
        if item['families'][family]['average_precision'] is not None])) for family in families}
    save(OUT / 'cal500.ranking.json', report)
    lines = ['# CAL500 held-out activity ranking', '',
             f"{len(rows)} audio examples. Macro activity AP: attributes "
             f"{report['macro_activity_average_precision']['attributes']:.3f}; embeddings "
             f"{report['macro_activity_average_precision']['embedding']:.3f}.", '',
             'Each activity has its own prevalence baseline. Lift compares average published soft annotation weight '
             'among the ten highest-ranked tracks in each held-out fold with random selection.', '',
             '| Activity | Binary positives | Prevalence | Attribute AP | Embedding AP | Embedding AUC | Top-ten vote lift |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    def number(value):
        return 'undefined' if value is None else f'{value:.3f}'
    for name, item in report['activities'].items():
        attribute, embedding = item['families']['attributes'], item['families']['embedding']
        lines.append(f"| {name[6:].replace('_', ' ')} | {item['positives']} | {item['prevalence']:.3f} | "
                     f"{number(attribute['average_precision'])} | {number(embedding['average_precision'])} | "
                     f"{number(embedding['roc_auc'])} | {number(embedding['top_ten_vote_lift_vs_random'])} |")
    lines.extend(['', *['- ' + value for value in report['limitations']], ''])
    (OUT / 'cal500.ranking.md').write_text('\n'.join(lines))
    print(json.dumps({'macro_activity_average_precision': report['macro_activity_average_precision']}))


if __name__ == '__main__':
    main()
