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
from yt_playlist.rec import artist_model, embed, rec_params, taste_modes
from yt_playlist.util import genre_catalog
from yt_playlist.repos.rec_refresh import VERSION


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        default=lambda value: hashlib.sha256(value).hexdigest() if isinstance(value, bytes)
                              else sorted(value)).encode()).hexdigest()


def prepare(reader, now, *, keep_collaborative=False):
    """Return serialized writes; never change the reader or the serving model."""
    method = reader.settings.get_setting("rec_embed_method") or "svd"
    dim = int(reader.settings.get_setting("rec_dim") or embed.DIM)
    baskets = reader.rec_query.rec_baskets()
    weights = (embed._cooc_weights(reader)
               if reader.settings.get_setting("rec_cooc_weighting") == "1" else None)
    fit_inputs = _fingerprint([method, dim, sorted(sorted(set(b)) for b in baskets), weights])
    settings, vectors = {"rec_refresh_version": VERSION}, {}
    if keep_collaborative:
        settings["rec_fit_inputs"] = fit_inputs
    elif reader.settings.get_setting("rec_fit_inputs") != fit_inputs:
        keys, matrix = embed.build_vectors_from_baskets(baskets, dim=dim, method=method, weights=weights)
        vectors["rec_vectors"] = [(key, matrix[i].tobytes()) for i, key in enumerate(keys)]
        settings["rec_fit_inputs"] = fit_inputs

    previous = reader.rec_model.content_generation()
    inputs = embed._content_inputs(reader)
    content_inputs = embed._content_input_fingerprint(inputs)
    content_changed = (not previous["model"] or previous["inputs"] != content_inputs
                       or previous["revision"] != genre_catalog.REVISION)
    generation = previous
    if content_changed:
        keys, matrix, model = embed._build_library_content(reader, inputs=inputs)
        generation = {"generation": uuid.uuid4().hex, "model": json.dumps(model),
                      "revision": genre_catalog.REVISION, "inputs": content_inputs,
                      "library": [(key, matrix[i].tobytes()) for i, key in enumerate(keys)],
                      "discovered": embed._encode_discovered(reader.discovery.get_discovered_tracks(), model)}
        vectors["rec_content_vectors"] = generation["library"]
        vectors["rec_discovered_content_vectors"] = generation["discovered"]
        settings.update(rec_content_model=generation["model"],
                        rec_content_generation=generation["generation"],
                        rec_content_genre_revision=generation["revision"],
                        rec_content_inputs=content_inputs)
    elif generation["model"]:
        # Discovery membership can change without changing the library's encoder.
        rows = embed._encode_discovered(reader.discovery.get_discovered_tracks(),
                                       json.loads(generation["model"]))
        if dict(rows) != dict(generation["discovered"]):
            generation = {**generation, "generation": uuid.uuid4().hex, "discovered": rows}
            vectors["rec_discovered_content_vectors"] = rows
            settings["rec_content_generation"] = generation["generation"]

    profiles = artist_model.artist_content_profiles(reader)
    for artist, profile in artist_model._discovered_artist_profiles(reader).items():
        profiles.setdefault(artist, profile)
    artist_inputs = _fingerprint([artist_model.artist_baskets(reader), profiles])
    if reader.settings.get_setting("rec_artist_fit_inputs") != artist_inputs:
        keys, matrix = artist_model.build_collab_artist_vectors(reader)
        vectors["rec_artist_vectors"] = [(key, matrix[i].tobytes()) for i, key in enumerate(keys)]
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


def rebuild(store, now, *, keep_collaborative=False):
    """Return a completed batch, or None when a source edit superseded its inputs."""
    revision = store.rec_refresh.revision()
    with store.read_snapshot() as snapshot:
        # Fits consume catalog identity, genre and audio, never enrichment's
        # scheduling stamps. Those stamps change throughout provider catch-up
        # and otherwise reject every full fit despite unchanged model inputs.
        with column_group_reads(snapshot.store, "recordings", "catalog"):
            prepared = prepare(snapshot.store, now, keep_collaborative=keep_collaborative)
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
