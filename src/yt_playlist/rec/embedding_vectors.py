"""Vector generation reads, decoding and guarded discovery re-encoding.

Readers return detached VectorSpace values. Fitting stays in embedding_training
and content_model; maintenance owns complete generation publication.
"""
import json
from typing import NamedTuple

import numpy as np

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import content_model


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


def content_inputs(store) -> content_model.ContentInputs:
    dao = store.rec_query
    return content_model.ContentInputs(sorted(dao.library_keys()), dao.track_content(), dao.track_audio_features())


def content_space_id(store, *, snapshot=None) -> str:
    raw = snapshot["model"] if snapshot is not None else store.settings.get_setting("rec_content_model")
    return content_model.content_model_fingerprint(json.loads(raw)) if raw else ""


def build_discovered_content_vectors(store) -> int:
    """Re-encode only against one model generation, retrying competing publication."""
    for _ in range(3):
        snapshot = store.rec_model.content_generation()
        if not snapshot["model"]:
            return 0
        out = content_model.encode_discovered(store.discovery.get_discovered_tracks(), json.loads(snapshot["model"]))
        if store.rec_model.publish_content_generation(snapshot["library"], out, snapshot["model"],
                snapshot["revision"] or "", snapshot["inputs"], snapshot["generation"]):
            return len(out)
    raise RuntimeError("Content generation changed during discovery encoding; retry the refresh")


def load_content_vectors(store, *, snapshot=None) -> VectorSpace:
    return load_vector_rows(snapshot["library"] if snapshot is not None else store.rec_model.get_rec_content_vectors())


def load_discovered_content_vectors(store, *, snapshot=None) -> VectorSpace:
    return load_vector_rows(snapshot["discovered"] if snapshot is not None else store.rec_model.get_rec_discovered_content_vectors())


def content_space(store, include_new) -> VectorSpace:
    """Load both library and discovery rows from the same committed content model."""
    snapshot = store.rec_model.content_generation()
    ckeys, CV, cidx = load_content_vectors(store, snapshot=snapshot)
    if include_new:
        dkeys, DV, _ = load_discovered_content_vectors(store, snapshot=snapshot)
        if DV is not None:
            if CV is None:
                ckeys, CV = list(dkeys), DV
            elif DV.shape[1] == CV.shape[1]:
                ckeys = list(ckeys) + list(dkeys)
                CV = np.vstack([CV, DV])
        cidx = {k: i for i, k in enumerate(ckeys)}
    return VectorSpace(ckeys, CV, cidx)
