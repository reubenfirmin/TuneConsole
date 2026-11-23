"""Capture model and facet facts for local recommendation scores."""
from yt_playlist.rec import embedding_vectors as vectors
from yt_playlist.rec.scoring import (_apply_axis_weights, content_taste,
                                     personalized_track_components, score_with_traces)
from yt_playlist.rec.surface_values import _catalog_scores


def score_candidates(store, taste, keys, matrix, index, now, include_traces=False, *, mix_listening=None):
    if include_traces:
        return score_with_traces(store, taste, keys, matrix, index, now)
    scores = personalized_track_components(store, taste, matrix, index, now,
                                           mix_listening=mix_listening)['combined']
    return _apply_axis_weights(store, {keys[i]: float(scores[i]) for i in range(len(keys))}, now,
                               recent={} if mix_listening is not None else None)


def content_scores(store, plays, now, *, mix_listening=None):
    snapshot = store.rec_model.content_generation()
    taste = content_taste(store, snapshot=snapshot, now=now)
    if not taste:
        return {}
    keys, matrix, index = vectors.load_content_vectors(store, snapshot=snapshot)
    if matrix is None:
        return {}
    _, _, collaborative = vectors.load_vectors(store)
    owned = set(store.rec_query.library_keys())
    candidates = [(i, key) for i, key in enumerate(keys) if key in owned and key not in collaborative]
    if not candidates:
        return {}
    evidence = (vectors.content_space(store, include_new=True, snapshot=snapshot)
                if mix_listening is None else None)
    scores = personalized_track_components(store, taste, matrix, index, now, content_only=True,
                                           evidence_vecs=(evidence.matrix, evidence.index) if evidence else None,
                                           mix_listening=mix_listening)['combined']
    fit = _apply_axis_weights(store, {key: float(scores[i]) for i, key in candidates}, now,
                              recent={} if mix_listening is not None else None)
    return _catalog_scores(fit, plays)
