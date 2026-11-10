"""Independent acceptance of the retained external-human Focus predictions.

This reads only frozen JSON, fits nothing and never opens an application DB.
The per-row expected-discount calculation does not import the scorer or metric.
"""
import hashlib
import json
import math
from pathlib import Path

import pytest


ROUND = Path(__file__).resolve().parents[2] / 'audio_lab/local/mapped-vibes-20261029/round-1'
PLAN_SHA = '5d8744ca36ce0037d47f756333e26ba5f04198c0faa1a4b56c9f6e51d49a32dc'


def test_frozen_focus_retains_fractional_human_targets_and_reported_rankings(tmp_path):
    names = ('plan.json', 'cal500-raw-activity.predictions.json',
             'cal500-raw-activity.report.json', 'dataset-cal500-raw-activity.receipt.json')
    before = {name: hashlib.sha256((ROUND / name).read_bytes()).hexdigest() for name in names}
    assert before['plan.json'] == PLAN_SHA
    predictions, report, receipt = [json.loads((ROUND / name).read_text()) for name in names[1:]]
    assert receipt['fingerprint'] == PLAN_SHA and receipt['state'] == 'complete'
    for name in names[1:3]:
        assert receipt['outputs'][name] == before[name]
    targets = predictions['native_target_names']
    reading, studying = targets.index('Usage-Reading'), targets.index('Usage-Studying')
    working = targets.index('Usage-At_work')
    rows = predictions['rows']
    assert len(rows) == 494 and len({row['group'] for row in rows}) == 491
    group_folds = {}
    for row in rows:
        group_folds.setdefault(row['group'], set()).add(row['outer_fold'])
        native = row['native_observed']
        assert row['vibes']['concentrate']['observed'] == (native[reading] + native[studying]) / 2
        assert row['vibes']['concentrate_work_ablation']['observed'] == (
            native[reading] + native[studying] + native[working]) / 3
    assert all(len(folds) == 1 for folds in group_folds.values())
    assert len({row['vibes']['concentrate']['observed'] for row in rows}) > 4

    observed = {}
    for method, published in report['vibes']['concentrate'].items():
        folds = {}
        for fold in range(5):
            part = [row['vibes']['concentrate'] for row in rows if row['outer_fold'] == fold]
            relevance = [row['observed'] for row in part]
            scores = [row['predictions'][method] for row in part]
            k = min(10, math.ceil(len(part) / 5))
            discounts = [1 / math.log2(position + 2) for position in range(k)]
            ideal = sum(value * weight for value, weight in zip(sorted(relevance, reverse=True), discounts))
            dcg, top = 0., 0.
            for value, score in zip(relevance, scores):
                preceding = sum(other > score for other in scores)
                tied = sum(other == score for other in scores)
                dcg += value * sum(discounts[preceding:min(k, preceding + tied)]) / tied
                top += value * max(0, min(k, preceding + tied) - preceding) / tied / k
            average = sum(relevance) / len(relevance)
            actual = {'ndcg': dcg / ideal, 'random_ndcg': average * sum(discounts) / ideal,
                      'top_mean': top, 'random_top_mean': average}
            for key, value in actual.items():
                assert value == pytest.approx(published['folds'][str(fold)][key], abs=1e-12)
            folds[str(fold)] = actual
        aggregate = {key: sum(part[key] for part in folds.values()) / 5 for key in actual}
        for key, value in aggregate.items():
            assert value == pytest.approx(published[key], abs=1e-12)
        observed[method] = aggregate
    assert observed['selected']['ndcg'] < observed['prior_embedding_mae']['ndcg']
    assert observed['selected']['ndcg'] > observed['selected']['random_ndcg']
    after = {name: hashlib.sha256((ROUND / name).read_bytes()).hexdigest() for name in names}
    assert before == after
    (tmp_path / 'frozen-focus-acceptance.json').write_text(json.dumps({
        'rows': len(rows), 'groups': len(group_folds), 'input_hashes': before,
        'source_unchanged': before == after, 'methods': observed,
        'scope': 'external frozen human references; no training, DB, local labels or listening claims',
    }, indent=2) + '\n')
