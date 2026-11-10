"""Detached content vocabulary, audio projection and stable model identity.

Model dictionaries retain their persisted JSON shape. Inputs and fitted outputs
are named tuples, preserving the established ordering and unpacking contracts.
"""
import hashlib
import json
from typing import NamedTuple, TypedDict

import numpy as np

from yt_playlist.core.activity import check_stopping
from yt_playlist.util import genre_map

CONTINUOUS_AUDIO = ("bpm", "energy", "danceability", "mood_happy", "mood_sad", "mood_relaxed",
                    "mood_acoustic", "instrumental", "loudness", "dynamic_complexity")
AUDIO_DIM_W = 0.5


class ContentInputs(NamedTuple):
    keys: list[str]
    content: dict
    audio: dict


class ContentModel(TypedDict):
    cat: dict[str, int]
    ncat: int
    cont: list


class EncodedLibrary(NamedTuple):
    keys: list[str]
    matrix: np.ndarray
    model: ContentModel


def _genre_axes(genre, genre_axes):
    if genre_axes is not None:
        return genre_axes[genre]
    return genre_map.family(genre), genre_map.subgenre(genre)


def content_features(content, *, genre_axes=None):
    """Categorical column vocabulary and each track's active genre/decade columns."""
    feats: dict = {}

    def col(tok):
        return feats.setdefault(tok, len(feats))

    rows = []
    for k, (genre, year) in content.items():
        active = []
        if genre:
            family, sub = _genre_axes(genre,genre_axes)
            active.append(col(f"fam:{family}"))
            if sub:
                active.append(col(f"sub:{sub}"))
        if year and str(year)[:4].isdigit():
            active.append(col(f"dec:{int(str(year)[:4]) // 10 * 10}"))
        if active:
            rows.append((k, active))
    return feats, rows


def build_content_model(content, audio, *, genre_axes=None) -> ContentModel:
    """Fit ordered genre/era/key vocabulary and nonconstant audio z-score statistics."""
    cat, _ = content_features(content,genre_axes=genre_axes)
    cat = dict(cat)

    def cat_col(tok):
        return cat.setdefault(tok, len(cat))

    for d in audio.values():
        if d.get("music_scale"):
            cat_col(f"scale:{str(d['music_scale']).lower()}")
        if d.get("music_key"):
            cat_col(f"key:{d['music_key']}")
    cont = []
    for f in CONTINUOUS_AUDIO:
        vals = [float(d[f]) for d in audio.values() if d.get(f) is not None]
        if len(vals) >= 2:
            mu = float(np.mean(vals))
            sd = float(np.std(vals))
            if sd > 0:
                cont.append([f, mu, sd])
    return {"cat": cat, "ncat": len(cat), "cont": cont}


def encode_content(model: ContentModel, genre, year, audio, *, genre_axes=None):
    """Unit float32 projection, or None without any feature the model recognizes."""
    cat, ncat, cont = model["cat"], model["ncat"], model["cont"]
    vec = np.zeros(ncat + len(cont), dtype=np.float32)
    toks = []
    if genre:
        family, sub = _genre_axes(genre,genre_axes)
        toks.append(f"fam:{family}")
        if sub:
            toks.append(f"sub:{sub}")
    if year and str(year)[:4].isdigit():
        toks.append(f"dec:{int(str(year)[:4]) // 10 * 10}")
    a = audio or {}
    if a.get("music_scale"):
        toks.append(f"scale:{str(a['music_scale']).lower()}")
    if a.get("music_key"):
        toks.append(f"key:{a['music_key']}")
    for t in toks:
        c = cat.get(t)
        if c is not None:
            vec[int(c)] = 1.0
    for j, (f, mu, sd) in enumerate(cont):
        val = a.get(f)
        if val is not None and sd:
            vec[ncat + j] = AUDIO_DIM_W * (float(val) - mu) / sd
    n = float(np.linalg.norm(vec))
    return (vec / n).astype(np.float32) if n > 0 else None


def input_fingerprint(inputs: ContentInputs, *, genre_axes=None):
    axes = {genre: _genre_axes(genre,genre_axes)
            for genre in {genre for genre,_year in inputs.content.values()} if genre} if genre_axes is not None else None
    return hashlib.sha256(json.dumps([inputs,axes] if axes is not None else inputs, sort_keys=True).encode()).hexdigest()


def build_library_content(inputs: ContentInputs, *, genre_axes=None) -> EncodedLibrary:
    """Fit and encode detached library inputs in one shared content space."""
    _, content, audio = inputs
    model = build_content_model(content, audio,genre_axes=genre_axes)
    candidates = sorted(set(content) | set(audio))
    matrix = np.empty((len(candidates), model['ncat'] + len(model['cont'])), dtype=np.float32)
    keys = []
    for i, k in enumerate(candidates):
        if i % 256 == 0:
            check_stopping()
        genre, year = content.get(k, (None, None))
        v = encode_content(model, genre, year, audio.get(k),genre_axes=genre_axes)
        if v is not None:
            matrix[len(keys)] = v
            keys.append(k)
    V = matrix[:len(keys)] if keys else np.zeros((0, 0), dtype=np.float32)
    return EncodedLibrary(keys, V, model)


def content_model_fingerprint(model) -> str:
    """Identify ordered column meanings, excluding drifting audio means/stddevs."""
    payload = json.dumps({
        "catalog_identity_version": 2,
        "cat": sorted(model.get("cat", {}).items(), key=lambda kv: (kv[1], kv[0])),
        "cont": [c[0] for c in model.get("cont", [])],
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def encode_discovered(rows, model: ContentModel, *, genre_axes=None):
    out = []
    for i, r in enumerate(rows):
        if i % 256 == 0:
            check_stopping()
        v = encode_content(model, r.get("genre"), r.get("year"), r.get("audio"),genre_axes=genre_axes)
        if v is not None:
            out.append((r["identity_key"], v.tobytes()))
    return out
