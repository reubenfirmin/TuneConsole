"""Capture model and facet facts for local recommendation scores."""
from yt_playlist.rec import embedding_vectors as vectors
from yt_playlist.rec.scoring import _apply_axis_weights, _apply_mood, content_taste, score_with_traces
from yt_playlist.rec.surface_values import _catalog_scores


def score_candidates(store, taste, keys, matrix, index, now, include_traces=False):
    if include_traces:
        return score_with_traces(store, taste, keys, matrix, index, now)
    scores = _apply_mood(taste.allocated_scores(matrix), store, now, matrix, index)
    return _apply_axis_weights(store, {keys[i]: float(scores[i]) for i in range(len(keys))}, now)


def content_scores(store, plays, now):
    snapshot = store.rec_model.content_generation()
    taste = content_taste(store, snapshot=snapshot, now=now)
    if not taste:
        return {}
    keys, matrix, _ = vectors.load_content_vectors(store, snapshot=snapshot)
    if matrix is None:
        return {}
    _, _, index = vectors.load_vectors(store)
    owned = set(store.rec_query.library_keys())
    candidates = [(i, key) for i, key in enumerate(keys) if key in owned and key not in index]
    if not candidates:
        return {}
    scores = taste.allocated_scores(matrix)
    fit = _apply_axis_weights(store, {key: float(scores[i]) for i, key in candidates}, now)
    return _catalog_scores(fit, plays)
