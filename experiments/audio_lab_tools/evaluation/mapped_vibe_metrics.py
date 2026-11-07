"""Graded, tie-aware ranking and proper squared loss for bounded annotations."""
import math

import numpy as np
from scipy.stats import rankdata


def top_count(n):
    return min(10, max(1, math.ceil(.2 * n)))


def discounted_gain(y, score, k):
    """Linear relevance; average gain over every ordering within each score tie."""
    y, score = np.asarray(y), np.asarray(score)
    order = np.argsort(-score, kind='stable')
    sorted_scores = score[order]
    gain, top = 0., 0.
    start = 0
    while start < len(order) and start < k:
        end = start + 1
        while end < len(order) and sorted_scores[end] == sorted_scores[start]:
            end += 1
        relevance = float(y[order[start:end]].mean())
        stop = min(k, end)
        gain += relevance * np.sum(1 / np.log2(np.arange(start, stop) + 2))
        top += relevance * (stop - start)
        start = end
    return float(gain), float(top / k)


def ranking(y, score):
    y, score = np.asarray(y, dtype=float), np.asarray(score, dtype=float)
    if y.shape != score.shape or y.ndim != 1 or not len(y):
        raise ValueError('Expected nonempty equally shaped ranking vectors')
    if not np.isfinite(y).all() or not np.isfinite(score).all():
        raise ValueError('Nonfinite ranking inputs')
    k = top_count(len(y))
    varying = float(np.ptp(y)) > 1e-12
    ideal, _ = discounted_gain(y, y, k)
    gain, top = discounted_gain(y, score, k)
    random_gain, _ = discounted_gain(y, np.zeros(len(y)), k)
    correlation = None
    if varying and np.ptp(score) > 1e-12:
        correlation = float(np.corrcoef(rankdata(y), rankdata(score))[0, 1])
    return {'rows': len(y), 'k': k, 'evaluable': varying,
            'ndcg': gain / ideal if varying and ideal > 0 else None,
            'random_ndcg': random_gain / ideal if varying and ideal > 0 else None,
            'top_mean': top, 'random_top_mean': float(y.mean()),
            'top_lift': top - float(y.mean()) if varying else None,
            'spearman': correlation, 'mse': float(np.mean((y - score) ** 2)),
            'mae': float(np.mean(np.abs(y - score)))}


def summary(y, score, split):
    parts = {str(f): ranking(y[split == f], score[split == f]) for f in sorted(set(split))}
    result = {'folds': parts, 'evaluable_folds': sum(p['evaluable'] for p in parts.values())}
    for key in ('ndcg', 'random_ndcg', 'top_mean', 'random_top_mean', 'top_lift', 'spearman', 'mse', 'mae'):
        values = [p[key] for p in parts.values() if p[key] is not None]
        result[key] = float(np.mean(values)) if values else None
    result['ndcg_gain'] = (result['ndcg'] - result['random_ndcg']
                           if result['ndcg'] is not None else None)
    result['folds_above_random'] = sum(p['ndcg'] is not None and p['ndcg'] > p['random_ndcg'] + 1e-12
                                      for p in parts.values())
    return result


def choose(y, scores, split, allowed=None):
    """Training-only selection: mean fold NDCG, then proper MSE, then grid order."""
    allowed = list(range(scores.shape[1])) if allowed is None else list(allowed)
    evidence = [summary(y, scores[:, i], split) for i in allowed]
    def key(pair):
        index, item = pair
        return (round(item['ndcg'], 12) if item['ndcg'] is not None else -1.,
                -round(item['mse'], 12), -index)
    winner = max(zip(allowed, evidence), key=key)[0]
    return winner, {str(i): {'ndcg': item['ndcg'], 'mse': item['mse']}
                    for i, item in zip(allowed, evidence)}
