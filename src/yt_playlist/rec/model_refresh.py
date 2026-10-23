"""Fit the established models privately and publish one coherent generation.

The worker owns scheduling. This module keeps full fits off the serving connection:
the SVD/SGNS objective, content encoder, artist model and cluster matching retain
their existing implementations. Requests keep using the last published generation.
"""
import hashlib
import json
import uuid

from yt_playlist.core.activity import check_stopping
from yt_playlist.core.db_revisions import column_group_reads
from yt_playlist.rec import (artist_model, content_model, embedding_training as training,
                             embedding_vectors as vector_reads, rec_params, taste_modes)
from yt_playlist.util import genre_catalog
from yt_playlist.repos.rec_refresh import VERSION


class MemoryBudgetExceeded(RuntimeError):
    """A complete fit would exceed its admitted working-set estimate."""


class FitCache:
    """Bounded detached SVD/SGNS work retained across deferred publications.

    One worker owns this cache. Every lookup follows a fresh guarded input read;
    nothing in the cache can publish itself or bypass snapshot validation.
    """
    def __init__(self, memory_bytes=64 * 1024 * 1024):
        self.memory_bytes = memory_bytes
        self.entries = {}
        self.sizes = {}

    def vectors(self, kind, fingerprint, prepare):
        key = (kind, fingerprint)
        if key in self.entries:
            return self.entries[key]
        rows = prepare()
        size = sum(128 + len(identity.encode()) + len(blob) for identity, blob in rows)
        if size <= self.memory_bytes:
            while self.entries and (len(self.entries) >= 2 or sum(self.sizes.values()) + size > self.memory_bytes):
                oldest = next(iter(self.entries))
                del self.entries[oldest], self.sizes[oldest]
            self.entries[key], self.sizes[key] = rows, size
        return rows


def _check_fit_memory(baskets, dim, memory_bytes):
    if memory_bytes is None:
        return
    n = len({key for basket in baskets for key in basket})
    # Conservative nonzero bound for sparse co-occurrence/PPMI intermediates,
    # dense SVD sketches and detached basket storage. Check before multiplying
    # incidence matrices, where even one broad basket can become expensive.
    pairs = min(n * n, sum(len(basket) * (len(basket) - 1) for basket in baskets))
    estimated = pairs * 64 + n * (dim + 10) * 64 + sum(map(len, baskets)) * 128
    if estimated > memory_bytes:
        raise MemoryBudgetExceeded(f'Complete fit estimate {estimated} exceeds {memory_bytes} bytes')


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        default=lambda value: hashlib.sha256(value).hexdigest() if isinstance(value, bytes)
                              else sorted(value)).encode()).hexdigest()


def prepare(reader, now, *, keep_collaborative=False, memory_bytes=None, fit_cache=None):
    """Return serialized writes; never change the reader or the serving model."""
    method = reader.settings.get_setting("rec_embed_method") or "svd"
    dim = int(reader.settings.get_setting("rec_dim") or training.DIM)
    baskets = reader.rec_evidence.rec_baskets()
    weights = (training.cooc_weights(reader.rec_query.play_counts())
               if reader.settings.get_setting("rec_cooc_weighting") == "1" else None)
    fit_inputs = _fingerprint([method, dim, sorted(sorted(set(b)) for b in baskets), weights])
    settings, vectors = {"rec_refresh_version": VERSION}, {}
    if keep_collaborative:
        settings["rec_fit_inputs"] = fit_inputs
    elif reader.settings.get_setting("rec_fit_inputs") != fit_inputs:
        _check_fit_memory(baskets, dim, memory_bytes)
        def fit_tracks():
            keys, matrix = training.build_vectors_from_baskets(baskets, dim=dim, method=method, weights=weights)
            return [(key, matrix[i].tobytes()) for i, key in enumerate(keys)]
        vectors['rec_vectors'] = (fit_cache.vectors('tracks', fit_inputs, fit_tracks)
                                  if fit_cache is not None else fit_tracks())
        settings["rec_fit_inputs"] = fit_inputs

    previous = reader.rec_model.content_generation()
    inputs = vector_reads.content_inputs(reader)
    content_inputs = content_model.input_fingerprint(inputs)
    content_changed = (not previous["model"] or previous["inputs"] != content_inputs
                       or previous["revision"] != genre_catalog.REVISION)
    generation = previous
    if content_changed:
        if memory_bytes is not None:
            model = content_model.build_content_model(inputs.content, inputs.audio)
            estimated = (len(inputs.keys) * (2048 + (model['ncat'] + len(model['cont'])) * 32))
            if estimated > memory_bytes:
                raise MemoryBudgetExceeded(f'Content fit estimate {estimated} exceeds {memory_bytes} bytes')
        keys, matrix, model = content_model.build_library_content(inputs)
        generation = {"generation": uuid.uuid4().hex, "model": json.dumps(model),
                      "revision": genre_catalog.REVISION, "inputs": content_inputs,
                      "library": [(key, matrix[i].tobytes()) for i, key in enumerate(keys)],
                      "discovered": content_model.encode_discovered(reader.discovery.get_discovered_tracks(), model)}
        vectors["rec_content_vectors"] = generation["library"]
        vectors["rec_discovered_content_vectors"] = generation["discovered"]
        settings.update(rec_content_model=generation["model"],
                        rec_content_generation=generation["generation"],
                        rec_content_genre_revision=generation["revision"],
                        rec_content_inputs=content_inputs)
    elif generation["model"]:
        # Discovery membership can change without changing the library's encoder.
        rows = content_model.encode_discovered(reader.discovery.get_discovered_tracks(),
                                       json.loads(generation["model"]))
        if dict(rows) != dict(generation["discovered"]):
            generation = {**generation, "generation": uuid.uuid4().hex, "discovered": rows}
            vectors["rec_discovered_content_vectors"] = rows
            settings["rec_content_generation"] = generation["generation"]

    profiles = artist_model.artist_content_profiles(reader)
    for artist, profile in artist_model._discovered_artist_profiles(reader).items():
        profiles.setdefault(artist, profile)
    artist_baskets = artist_model.artist_baskets(reader)
    artist_inputs = _fingerprint([artist_baskets, profiles])
    if reader.settings.get_setting("rec_artist_fit_inputs") != artist_inputs:
        _check_fit_memory(artist_baskets, training.DIM, memory_bytes)
        def fit_artists():
            keys, matrix = artist_model.build_collab_artist_vectors(reader)
            return [(key, matrix[i].tobytes()) for i, key in enumerate(keys)]
        artist_collab_inputs = _fingerprint([training.DIM, artist_baskets])
        vectors['rec_artist_vectors'] = (fit_cache.vectors('artists', artist_collab_inputs, fit_artists)
                                         if fit_cache is not None else fit_artists())
        model = artist_model.build_artist_content_model(profiles)
        rows = []
        for artist in sorted(profiles):
            vector = artist_model.encode_artist_content(model, profiles[artist])
            if vector is not None:
                rows.append((artist, vector.tobytes()))
        vectors["rec_artist_content_vectors"] = rows
        settings.update(rec_artist_fit_inputs=artist_inputs, rec_artist_content_model=json.dumps(model))

    mode_inputs = _fingerprint([generation["model"], sorted(generation["library"]),
        {name: rec_params.get_param(reader, name) for name in
         ("modes_k", "modes_min_members", "modes_match_threshold")}])
    modes = None
    if reader.settings.get_setting("rec_mode_fit_inputs") != mode_inputs:
        modes = taste_modes.prepare_modes(reader, now, snapshot=generation)
        if modes is not None:
            upserts, retired = modes
            modes = ([(m["mode_id"], m["label"], json.dumps(m["families"]), m["centroid"].tobytes(),
                       m["size"], json.dumps(m["rep_keys"]), now, now, m["space"]) for m in upserts], retired)
        settings["rec_mode_fit_inputs"] = mode_inputs
    check_stopping()
    return {"vectors": vectors, "settings": settings, "modes": modes,
            "tracks": len(vectors.get("rec_vectors", ())) if "rec_vectors" in vectors
                      else reader.rec_model.rec_vectors_count()}


def rebuild(store, now, *, keep_collaborative=False, memory_bytes=None, fit_cache=None):
    """Return a completed batch, or None when a source edit superseded its inputs."""
    revision = store.rec_refresh.revision()
    with store.read_snapshot() as snapshot:
        # Fits consume catalog identity, genre and audio, never enrichment's
        # scheduling stamps. Those stamps change throughout provider catch-up
        # and otherwise reject every full fit despite unchanged model inputs.
        with column_group_reads(snapshot.store, "recordings", "catalog"):
            prepared = prepare(snapshot.store, now, keep_collaborative=keep_collaborative,
                               memory_bytes=memory_bytes, fit_cache=fit_cache)
        changes = snapshot.store.rec_model.prepare_training_changes(prepared['vectors'])
        prepared['vector_changes'] = changes
        def publish():
            store.rec_model.publish_training_generation(changes)
            store.settings.publish_values(prepared['settings'])
            if prepared['modes'] is not None:
                store.modes.publish_generation(*prepared['modes'])
            store.rec_refresh.acknowledge(revision)
        if not snapshot.publish(publish):
            return None
        return prepared
