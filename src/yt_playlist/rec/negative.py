"""Durable, bounded steering away from the current explicit dislike set.

The persisted dislike rows outlive transient mood pruning and the track's temporary ban.
Clearing a dislike removes its direction immediately. No genre/era weights are written here.
"""
import numpy as np

from yt_playlist.rec import embedding_vectors as vector_reads, rec_params


def _disliked_rows(store, lookup):
    """Acquisition: the evidence-space row for every currently disliked key present in `lookup`."""
    return [lookup[k] for k in sorted(store.rec_model.disliked_identity_keys()) if k in lookup]


def _penalty_from_rows(vectors, evidence, rows, weight):
    """Pure: one nonpositive delta per candidate in `vectors`, from the mean unit direction of
    `evidence[rows]`.

    Average unit evidence vectors without normalizing the mean: conflicting dislikes cancel
    rather than amplifying noise. Opposite or orthogonal candidates get no penalty or reward.
    Missing/zero vectors are neutral, and the magnitude cannot exceed `weight`.
    """
    result = np.zeros(len(vectors), dtype=np.float64)
    if not weight or not rows:
        return result
    selected = np.asarray(evidence[rows], dtype=np.float64)
    norms = np.linalg.norm(selected, axis=1)
    selected = selected[norms > 0] / norms[norms > 0, None]
    if not len(selected):
        return result
    direction = selected.mean(axis=0)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    unit = np.divide(vectors, norms, out=np.zeros_like(vectors, dtype=np.float64), where=norms > 0)
    return -weight * np.clip(unit @ direction, 0.0, 1.0)


def penalties(store, vectors, index, *, evidence_vecs=None):
    """One nonpositive delta per candidate, in the supplied vector space. See `_penalty_from_rows`
    for the pure math; this resolves the evidence space and the disliked rows within it."""
    weight = rec_params.get_param(store, "dislike_durable_w")
    if not weight:
        return np.zeros(len(vectors), dtype=np.float64)
    evidence, lookup = (vectors, index) if evidence_vecs is None else evidence_vecs
    rows = _disliked_rows(store, lookup)
    return _penalty_from_rows(vectors, evidence, rows, weight)


def for_keys(store, keys):
    """Bounded live adjustments for prepared cards; reuse persisted collaborative vectors."""
    keys = tuple(keys)
    return Steering(store).for_keys(keys) if keys else {}


class Steering:
    """One mean direction per read snapshot, without retaining all disliked vectors."""

    def __init__(self, store):
        self.store = store
        self.weight = rec_params.get_param(store, 'dislike_durable_w')
        self.direction = None
        if not self.weight:
            return
        after, count = None, 0
        while True:
            from yt_playlist.core.activity import check_cancellable_work
            check_cancellable_work()
            keys = store.rec_model.disliked_key_page(after=after, limit=128)
            if not keys:
                break
            _, vectors, _ = vector_reads.load_vector_rows(list(store.rec_model.vectors_by_keys(keys).items()))
            if vectors is not None:
                vectors = np.asarray(vectors, dtype=np.float64)
                norms = np.linalg.norm(vectors, axis=1)
                valid = norms > 0
                unit = np.asarray(vectors[valid], dtype=np.float64) / norms[valid, None]
                if len(unit):
                    total = unit.sum(axis=0)
                    self.direction = total if self.direction is None else self.direction + total
                    count += len(unit)
            after = keys[-1]
            if len(keys) < 128:
                break
        if count:
            self.direction /= count

    def for_keys(self, keys):
        if self.direction is None:
            return {}
        result, keys = {}, sorted(set(keys))
        for offset in range(0, len(keys), 128):
            present, vectors, _ = vector_reads.load_vector_rows(list(
                self.store.rec_model.vectors_by_keys(keys[offset:offset + 128]).items()))
            if vectors is None:
                continue
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            unit = np.divide(vectors, norms, out=np.zeros_like(vectors, dtype=np.float64), where=norms > 0)
            deltas = -self.weight * np.clip(unit @ self.direction, 0.0, 1.0)
            result.update(zip(present, map(float, deltas)))
        return result
