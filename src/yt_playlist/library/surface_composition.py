"""Storage and model bindings for local recommendation queries."""
from yt_playlist.controllers.surface_queries import SurfaceQueries
from yt_playlist.rec import embedding_vectors, rec_params
from yt_playlist.library.surface_inputs import score_candidates, content_scores
from yt_playlist.rec.scoring import playlist_taste


def queries(store, *, mix_listening=None):
    return SurfaceQueries(store.rec_query, store.rec_candidates, store.rec_model, store.genres,
        parameter=lambda name: rec_params.get_param(store, name),
        taste=lambda now: playlist_taste(store, now=now), vectors=lambda: embedding_vectors.load_vectors(store),
        score=lambda *args, **kw: score_candidates(store, *args, **kw, mix_listening=mix_listening),
        content=lambda plays, now: content_scores(store, plays, now, mix_listening=mix_listening))
