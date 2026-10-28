"""Per-vibe and paired-uncertainty checks on saved out-of-fold predictions."""
import json

import numpy as np

from .external_experiment import OUT, digest, normalize, save, stamp
from .external_transfer import SNAPSHOT, VIBES, probabilities, score


def paired(y, baseline, candidate, groups):
    accepted = y > 0
    base, values = probabilities(baseline), probabilities(candidate)
    indices = np.arange(len(y))
    accuracy = accepted[indices, values.argmax(1)].astype(float) - accepted[indices, base.argmax(1)]
    loss = -np.log((base * accepted).sum(1)) + np.log((values * accepted).sum(1))
    group_names = sorted(set(groups))
    sums = np.array([[accuracy[np.array(groups) == group].sum(), loss[np.array(groups) == group].sum()]
                     for group in group_names])
    counts = np.array([sum(group == name for group in groups) for name in group_names])
    samples = np.random.default_rng(507).integers(len(group_names), size=(5000, len(group_names)))
    results = sums[samples].sum(1) / counts[samples].sum(1)[:, None]
    return {'top_choice_improvement': float(accuracy.mean()),
            'top_choice_conditional_95_percent_interval': np.quantile(results[:, 0], [.025, .975]).tolist(),
            'acceptable_loss_reduction': float(loss.mean()),
            'loss_reduction_conditional_95_percent_interval': np.quantile(results[:, 1], [.025, .975]).tolist(),
            'method': 'Paired artist-group bootstrap of saved outer predictions. Fitting/selection is not repeated; multiple comparisons and prior reuse of gold are not corrected.'}


def per_vibe(y, values):
    p = probabilities(values)
    chosen = p.argmax(1)
    output = {}
    for i, name in enumerate(VIBES):
        positives = y[:, i] > 0
        predicted = chosen == i
        output[name] = {'human_positive_songs': int(positives.sum()), 'predicted_top_choice_songs': int(predicted.sum()),
                        'recall_as_top_choice': float(predicted[positives].mean()) if positives.any() else None,
                        'precision_as_top_choice': float(positives[predicted].mean()) if predicted.any() else None,
                        'acceptable_top_choice_within_human_positive_songs': score(y[positives], p[positives]) if positives.any() else None,
                        'mean_probability_on_human_positive_songs': float(p[positives, i].mean()) if positives.any() else None}
    return output


def write():
    snapshot = json.loads(SNAPSHOT.read_text())
    rows = [row for row in snapshot['gold'] if len(row.get('embedding_values', [])) == 200]
    ids = [row['video_id'] for row in rows]
    y = np.array([[int(name in row['labels']) / len(row['labels']) for name in VIBES] for row in rows])
    groups = [normalize(row.get('artist', '')) or row['video_id'] for row in rows]
    evidence = {}
    baseline = None
    for path in sorted(OUT.glob('consensus-*.oof.json')):
        report = json.loads(path.read_text())
        if [row['video_id'] for row in report['rows']] != ids or not np.allclose(report['observed'], y):
            raise ValueError('Saved consensus rows do not align with frozen human snapshot')
        for name, predictions in report['predictions'].items():
            if name == 'expert:embedding' and '__' not in path.name:
                if baseline is not None and not np.allclose(baseline, predictions):
                    raise ValueError('Default embedding baseline changed between consensus reports')
                baseline = np.array(predictions)
            # Do not duplicate thousands of post-inspection subset winners here.
            if name.startswith('expert:') or name in ('selected_combination', 'selected_candidate', 'weighted_external', 'weighted_all'):
                evidence[path.stem + '/' + name] = np.array(predictions)
    if baseline is None:
        raise ValueError('Need a completed baseline consensus report')
    output = {'created_at': stamp(), 'snapshot_sha256': digest(SNAPSHOT), 'rows': len(rows),
              'gold_without_embedding': [row['video_id'] for row in snapshot['gold'] if len(row.get('embedding_values', [])) != 200],
              'baseline': score(y, baseline), 'baseline_per_vibe': per_vibe(y, baseline), 'comparisons': {}}
    unique = {}
    for label, p in evidence.items():
        if p.shape != y.shape or not np.isfinite(p).all():
            raise ValueError('Malformed diagnostic probabilities')
        key = np.round(p, 12).tobytes()
        if key in unique:
            output['comparisons'][label] = {'identical_to': unique[key]}
            continue
        unique[key] = label
        output['comparisons'][label] = {'metrics': score(y, p), 'versus_embedding': paired(y, baseline, p, groups),
                                        'per_vibe': per_vibe(y, p)}
    save(OUT / 'gold-diagnostics.json', output)
    print(json.dumps({'gold_diagnostic_methods': len(evidence), 'unique_predictions': len(unique),
                      'per_vibe_human_counts': {name: value['human_positive_songs'] for name, value in output['baseline_per_vibe'].items()}}), flush=True)
    return output


if __name__ == '__main__':
    write()
