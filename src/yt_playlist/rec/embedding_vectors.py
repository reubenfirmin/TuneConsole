"""Vector generation reads, decoding and guarded discovery re-encoding.

Readers return detached VectorSpace values. Fitting stays in embedding_training
and content_model; maintenance owns complete generation publication.
"""
import json
from typing import NamedTuple

import numpy as np

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import content_character, content_model


class VectorSpace(NamedTuple):
    keys: list[str]
    matrix: np.ndarray | None
    index: dict[str, int]


def load_vector_rows(rows) -> VectorSpace:
    """Decode ordered (key, float32 bytes) rows with bounded cancellation checks."""
    if not rows:
        return VectorSpace([], None, {})
    check_stopping()
    keys = [k for k, _ in rows]
    vectors = []
    for i, (_, vector) in enumerate(rows):
        if i % 256 == 0:
            check_stopping()
        vectors.append(np.frombuffer(vector, dtype=np.float32))
    V = np.stack(vectors)
    check_stopping()
    return VectorSpace(keys, V, {k: i for i, k in enumerate(keys)})


def load_vectors(store) -> VectorSpace:
    return load_vector_rows(store.rec_model.get_rec_vectors())


def capture_content_inputs(store, discoveries):
    dao = store.rec_query
    keys, content, catalog = sorted(dao.library_keys()), dao.track_content(), dao.track_audio_features()
    catalogs = {row['identity_key']: row.get('audio') for row in discoveries}
    catalogs.update({key: catalog.get(key) for key in keys})
    character = content_character.load(store, catalogs)
    audio = {key: content_character.audio(catalog.get(key), character[key][0]) for key in keys
             if catalog.get(key) or character[key][0]}
    inputs = content_model.ContentInputs(keys, content, audio, content_character.VERSION)
    return inputs, [{**row, 'character': character[row['identity_key']][0]} for row in discoveries], character


def content_inputs(store) -> content_model.ContentInputs:
    return capture_content_inputs(store, [])[0]


def discovery_inputs(store, rows):
    character = content_character.load(store, {row['identity_key']: row.get('audio') for row in rows})
    return [{**row, 'character': character[row['identity_key']][0]} for row in rows]


def content_space_id(store, *, snapshot=None) -> str:
    raw = snapshot["model"] if snapshot is not None else store.rec_model.recording_encoder()
    return content_model.content_model_fingerprint(json.loads(raw)) if raw else ""


def build_discovered_content_vectors(store) -> int:
    """Re-encode only against one model generation, retrying competing publication."""
    for _ in range(3):
        snapshot = store.rec_model.content_generation()
        if not snapshot["model"]:
            return 0
        rows = store.discovery.get_discovered_tracks()
        model = json.loads(snapshot["model"])
        if model.get('policy'):
            rows = discovery_inputs(store, rows)
        axes = store.genres.feature_axes(row.get('genre') for row in rows)
        out = content_model.encode_discovered(rows, model, genre_axes=axes)
        if store.rec_model.publish_content_generation(snapshot["library"], out, snapshot["model"],
                snapshot["revision"] or "", snapshot["inputs"], snapshot["generation"]):
            return len(out)
    raise RuntimeError("Content generation changed during discovery encoding; retry the refresh")


def load_content_vectors(store, *, snapshot=None) -> VectorSpace:
    return load_vector_rows(snapshot["library"] if snapshot is not None else store.rec_model.get_rec_content_vectors())


def load_discovered_content_vectors(store, *, snapshot=None) -> VectorSpace:
    return load_vector_rows(snapshot["discovered"] if snapshot is not None else store.rec_model.get_rec_discovered_content_vectors())


def content_space(store, include_new, *, snapshot=None) -> VectorSpace:
    """Load both library and discovery rows from the same committed content model."""
    if snapshot is None:
        snapshot = store.rec_model.content_generation()
    ckeys, CV, cidx = load_content_vectors(store, snapshot=snapshot)
    if include_new:
        dkeys, DV, _ = load_discovered_content_vectors(store, snapshot=snapshot)
        if DV is not None:
            if CV is None:
                ckeys, CV = list(dkeys), DV
            elif (CV.shape[1] == DV.shape[1]
                    and snapshot['library_basis'] == snapshot['discovered_basis']):
                added = [i for i, key in enumerate(dkeys) if key not in cidx]
                if added:
                    ckeys = [*ckeys, *(dkeys[i] for i in added)]
                    CV = np.vstack((CV, DV[added]))
        cidx = {k: i for i, k in enumerate(ckeys)}
    return VectorSpace(ckeys, CV, cidx)
