"""Audit supplied listener annotations and compare unfiltered activity targets."""
from collections import defaultdict
import json
import re

import numpy as np

from .external_experiment import DATA, EMOTIFY_TARGETS, OUT, digest, save, stamp, table
from .external_training import SEED, evaluate


def split_half(annotations, names, repeats=100):
    """Descriptive stability across random listener halves, not model accuracy."""
    rng = np.random.default_rng(SEED)
    values = []
    for _ in range(repeats):
        left, right = [], []
        for row in annotations:
            order = rng.permutation(len(row))
            cut = len(order) // 2
            left.append(row[order[:cut]].mean(axis=0))
            right.append(row[order[cut:]].mean(axis=0))
        left, right = np.array(left), np.array(right)
        values.append([float(np.corrcoef(a, b)[0, 1]) if a.std() and b.std() else np.nan
                       for a, b in zip(left.T, right.T)])
    values = np.array(values)
    return {name: {'median_correlation': float(np.nanmedian(values[:, i])),
                   'split_10_to_90_percentiles': np.nanquantile(values[:, i], [.1, .9]).tolist()}
            for i, name in enumerate(names)}


def emotify_audit():
    source = DATA / 'emotify/data.csv'
    tracks = defaultdict(list)
    for row in table(source):
        tracks[int(row['track id'])].append([float(row[name]) for name in EMOTIFY_TARGETS])
    arrays = [np.array(row) for _, row in sorted(tracks.items())]
    counts = [len(row) for row in arrays]
    return {'source_sha256': digest(source), 'tracks': len(arrays), 'responses': sum(counts),
            'responses_per_track_min_median_max': [min(counts), float(np.median(counts)), max(counts)],
            'split_half': split_half(arrays, EMOTIFY_TARGETS),
            'meaning': 'Induced emotion. Listener halves are split separately within each track; no listener IDs are supplied.'}


def cal500_audit():
    base = DATA / 'cal500/annotations-source'
    source_names = (base / 'songNames.txt').read_text().splitlines()
    vocabulary = (base / 'vocab.txt').read_text().splitlines()
    indices = [i for i, name in enumerate(vocabulary) if name.startswith('Usage-')]
    names = [vocabulary[i] for i in indices]
    soft = np.loadtxt(base / 'softAnnotations.txt', delimiter=',')[:, indices]
    hard = np.loadtxt(base / 'hardAnnotations.txt', delimiter=',')[:, indices]
    files = defaultdict(list)
    for path in sorted((base / 'annotations').glob('*.txt')):
        files[re.sub(r'_\d+\.txt$', '', path.name)].append(path)
    arrays, file_hashes = [], {}
    for name in source_names:
        rows = []
        for path in files[name]:
            responses = dict(re.findall(r'^(Usage-[^= ]+) = "([^"]*)"', path.read_text(), re.M))
            rows.append([int(responses.get(target) == 'Yes') for target in names])
            file_hashes[path.name] = digest(path)
        if len(rows) < 2:
            raise ValueError('Need multiple annotations for ' + name)
        arrays.append(np.array(rows))
    raw = np.array([row.mean(axis=0) for row in arrays])
    positive_votes = np.array([row.sum(axis=0) for row in arrays])
    reconstructed = np.where(positive_votes >= 2, raw, 0)
    if not np.allclose(soft, reconstructed, atol=1e-5):
        raise ValueError('Supplied activity soft targets do not match the audited two-vote rule')
    if not np.array_equal(hard.astype(bool), soft > 0):
        raise ValueError('Supplied activity binary targets differ from nonzero soft targets')
    cached = json.loads((OUT / 'cal500.features.json').read_text())
    lookup = {name: i for i, name in enumerate(source_names)}
    rows = []
    for original in cached['rows']:
        i = lookup[original['id'].removeprefix('cal500:')]
        rows.append({**original, 'targets': raw[i].tolist(), 'target_names': names,
                     'rater_count': len(arrays[i]),
                     'target_aggregation': 'Unfiltered Yes-checkbox count divided by response count; omitted checkbox treated as unselected.'})
    save(OUT / 'cal500-raw-activity.features.json', {**cached, 'dataset': 'cal500-raw-activity',
         'rows': rows, 'annotation_source_hashes': file_hashes,
         'annotation_policy': rows[0]['target_aggregation']})
    counts = [len(row) for row in arrays]
    return {'tracks': len(arrays), 'responses': sum(counts),
            'responses_per_track_min_median_max': [min(counts), float(np.median(counts)), max(counts)],
            'source_hashes': file_hashes,
            'published_rule_verified': 'Usage soft = Yes fraction if at least two Yes votes, else zero; hard = soft > 0.',
            'readme_discrepancy': 'The supplied README describes 80 percent consensus, but these supplied activity matrices do not implement that rule.',
            'single_yes_entries_discarded': int(((positive_votes == 1) & (soft == 0)).sum()),
            'activities': {name: {'unfiltered_positive_tracks': int((raw[:, j] > 0).sum()),
                                   'published_positive_tracks': int(hard[:, j].sum()),
                                   'unfiltered_mean_fraction': float(raw[:, j].mean())}
                           for j, name in enumerate(names)},
            'split_half': split_half(arrays, names)}


def main():
    audit = {'created_at': stamp(), 'cal500': cal500_audit(), 'emotify': emotify_audit(),
             'limitations': ['Split-half correlations describe random divisions of the available responses, not confidence intervals or an accuracy ceiling.',
                             'CAL500 omitted activity checkboxes are interpreted as unselected; explicit missing-response identities are unavailable.',
                             'Published benchmark targets remain unchanged. Unfiltered activity targets form a separate research task.']}
    save(OUT / 'annotation-audit.json', audit)
    print(json.dumps({'annotation_audit': {key: {k: v for k, v in item.items() if k not in ('source_hashes', 'split_half')}
                                          for key, item in audit.items() if isinstance(item, dict)}}), flush=True)
    report = evaluate('cal500-raw-activity')
    report['limitations'].append('Targets are unfiltered raw activity-checkbox fractions, a separate task from the published CAL500 benchmark.')
    report['annotation_audit_sha256'] = digest(OUT / 'annotation-audit.json')
    save(OUT / 'cal500-raw-activity.report.json', report)


if __name__ == '__main__':
    main()
