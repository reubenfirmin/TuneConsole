"""Shared mode sampling and card framing for legacy and typed pool readers."""
import random

import numpy as np

from yt_playlist.rec import mode_eval, rec_params

PROTO_SIZE = 12
ROTATION_POOL = PROTO_SIZE * 25
CARD_LABELS = {'wheelhouse': 'More in your wheelhouse', 'explore': 'From your catalog',
               'comfort': 'Comfort listening', 'fresh': 'Fresh songs'}
BAND_LABELS = {0: 'Throwback', 1: 'Time Flies', 2: 'Recent Picks'}


def ranker_for(epoch, mode_id, share):
    seed = (int(epoch) * 1_000_003 + int(mode_id)) & 0x7FFFFFFF
    return 'ppr' if random.Random(seed).random() < share else 'cosine'


def _mode_mood_weight(mode, leans):
    weighted = sum(leans.get(f'genre:{family}', 0.0) * count for family, count in mode.get('families', []))
    total = sum(count for _, count in mode.get('families', [])) or 1
    return min(4.0, max(0.05, 1.0 + weighted / total))


def thompson_mode_scores(stats, mode_ids, rng):
    """Sample acceptance posteriors once per mode, in their established order."""
    return {mid: rng.betavariate(*mode_eval.choice_posterior(*stats.get(mid, (0, 0)))) for mid in mode_ids}


def select_modes(store, modes, leans, epoch, n=4, stats=None, now_posterior=None):
    """Choose distinct modes using acceptance, library share, mood and diversity.

    Sampling is deterministic for fixed inputs. Absent NOW evidence performs no
    parameter reads and preserves the original quiet-hours behavior.
    """
    if not modes:
        return []
    rng = random.Random(epoch)
    cents = {mode['mode_id']: np.asarray(mode['centroid'], dtype=np.float64) for mode in modes}
    now_gain = rec_params.get_param(store, 'now_gain') if now_posterior else None
    samples = thompson_mode_scores(stats or {}, [mode['mode_id'] for mode in modes], rng)
    scores = {mode['mode_id']: samples[mode['mode_id']] * max(1, mode['size'])
              * _mode_mood_weight(mode, leans)
              * (1.0 + now_gain * now_posterior.get(mode['mode_id'], 0.0) if now_posterior else 1.0)
              for mode in modes}
    chosen = [max(scores, key=scores.get)]
    remaining = [mode['mode_id'] for mode in modes if mode['mode_id'] != chosen[0]]
    while len(chosen) < min(n, len(modes)):
        def diverse_score(mid):
            distance = min(1.0 - float(cents[mid] @ cents[other]) for other in chosen)
            return scores[mid] * (1.0 + max(0.0, min(2.0, distance)))
        nxt = max(remaining, key=diverse_score)
        chosen.append(nxt)
        remaining.remove(nxt)
    return chosen
