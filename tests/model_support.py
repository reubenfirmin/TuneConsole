"""Build isolated model fixtures using the production numerical algorithms.

These deliberately publish individual spaces for algorithm and saved-format tests.
Application lifecycle tests use rec.model_refresh for coherent publication instead.
"""
import json

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import (artist_model, content_model, embedding_training as training,
                             embedding_vectors as vector_reads)
from yt_playlist.util import genre_catalog


def build_track_model(store, dim=None):
    if dim is None:
        dim = int(store.settings.get_setting("rec_dim") or training.DIM)
    keys, vectors = training.build_vectors_from_baskets(
        store.rec_evidence.rec_baskets(), dim=dim,
        method=store.settings.get_setting("rec_embed_method") or "svd",
        weights=(training.cooc_weights(store.rec_query.play_counts())
                 if store.settings.get_setting("rec_cooc_weighting") == "1" else None))
    check_stopping()
    store.rec_model.replace_rec_vectors([(key, vectors[i].tobytes()) for i, key in enumerate(keys)])
    return len(keys)


def build_content_model(store, *, inputs=None):
    previous = store.rec_model.content_generation()
    inputs = inputs if inputs is not None else vector_reads.content_inputs(store)
    keys, vectors, model = content_model.build_library_content(inputs)
    discovered = content_model.encode_discovered(store.discovery.get_discovered_tracks(), model)
    if not store.rec_model.publish_content_generation(
            [(key, vectors[i].tobytes()) for i, key in enumerate(keys)], discovered, json.dumps(model),
            genre_catalog.REVISION, content_model.input_fingerprint(inputs), previous["generation"]):
        raise RuntimeError("Content generation changed during fixture construction")
    return len(keys)


def build_collaborative_artist_model(store, dim=training.DIM):
    artists, vectors = artist_model.build_collab_artist_vectors(store, dim)
    store.rec_model.replace_rec_artist_vectors([(key, vectors[i].tobytes()) for i, key in enumerate(artists)])
    return len(artists)


def build_artist_content_model(store):
    profiles = artist_model.artist_content_profiles(store)
    for artist, profile in artist_model._discovered_artist_profiles(store).items():
        profiles.setdefault(artist, profile)
    model = artist_model.build_artist_content_model(profiles)
    rows = []
    for artist in sorted(profiles):
        vector = artist_model.encode_artist_content(model, profiles[artist])
        if vector is not None:
            rows.append((artist, vector.tobytes()))
    store.rec_model.replace_rec_artist_content_vectors(rows)
    store.settings.set_setting("rec_artist_content_model", json.dumps(model))
    return len(rows)


def build_artist_model(store, dim=training.DIM):
    count = build_collaborative_artist_model(store, dim)
    build_artist_content_model(store)
    return count
