"""Durable, bounded steering away from the current explicit dislike set.

The persisted dislike rows outlive transient mood pruning and the track's temporary ban.
Clearing a dislike removes its direction immediately. No genre/era weights are written here.
"""
import numpy as np

from yt_playlist.rec import embedding_vectors as vector_reads, rec_params


def penalties(store, vectors, index, *, evidence_vecs=None):
    """One nonpositive delta per candidate, in the supplied vector space.

    Average unit evidence vectors without normalizing the mean: conflicting dislikes cancel
    rather than amplifying noise. Opposite or orthogonal candidates get no penalty or reward.
    Missing/zero vectors are neutral, and the magnitude cannot exceed dislike_durable_w.
    """
    result = np.zeros(len(vectors), dtype=np.float64)
    weight = rec_params.get_param(store, "dislike_durable_w")
    if not weight:
        return result
    evidence, lookup = (vectors, index) if evidence_vecs is None else evidence_vecs
    rows = [lookup[k] for k in sorted(store.rec_model.disliked_identity_keys()) if k in lookup]
    if not rows:
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


def for_keys(store, keys):
    """Bounded live adjustments for prepared cards; reuse persisted collaborative vectors."""
    keys = set(keys)
    disliked = store.rec_model.disliked_identity_keys()
    if not keys or not disliked:
        return {}
    _, vectors, index = vector_reads.load_vector_rows(list(store.rec_model.vectors_by_keys(keys | disliked).items()))
    if vectors is None:
        return {}
    present = sorted(keys & index.keys())
    candidates = vectors[[index[k] for k in present]]
    deltas = penalties(store, candidates, {k: i for i, k in enumerate(present)},
                       evidence_vecs=(vectors, index))
    return dict(zip(present, map(float, deltas)))
