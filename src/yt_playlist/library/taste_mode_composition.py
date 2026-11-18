"""Bind mode acquisition without giving the calculation layer a Store."""
from yt_playlist.controllers.taste_mode_acquisition import TasteModes
from yt_playlist.rec import embedding_vectors, rec_params


def modes(store):
    return TasteModes(model=store.rec_model, modes=store.modes, axes=store.genres.feature_axes,
        parameter=lambda name: rec_params.get_param(store, name),
        space=lambda snapshot: embedding_vectors.content_space_id(store, snapshot=snapshot),
        vectors=lambda snapshot: embedding_vectors.load_content_vectors(store, snapshot=snapshot))
