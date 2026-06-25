"""Audio-driven 'arc energy' for the energy-axis journeys (issue #37).

The energy journeys (energy_arc / warm_up / wind_down) used to order tracks by the curated
per-family `genre_map.energy()` constant, so every track in a family sat at the same energy and
the within-family ordering was arbitrary. This module computes a real, per-track, BPM-led
composite in [0,1] from the AcousticBrainz/Deezer audio features we store (bpm, energy,
danceability) and falls back through the candidate pool for the ~60% of tracks that carry no
features:

    track value -> subgenre average -> family average -> curated family constant -> 0.5

Averages are taken over the *current pool's* enriched tracks only (arc ordering is relative within
a playlist), and the pass is deterministic so a seeded re-save reproduces the same arc.

Pure: takes plain dicts, depends only on genre_map, so it is trivially testable.
"""
from yt_playlist.util import genre_map

# BPM-led blend (issue #37). Weights are re-normalized over whichever features a track actually has.
_WEIGHTS = {"bpm": 0.5, "energy": 0.3, "danceability": 0.2}
_BPM_LO, _BPM_HI = 60.0, 180.0   # normalization window; covers the vast majority of music


def _norm_bpm(bpm) -> float:
    """Map BPM onto [0,1] over _BPM_LO.._BPM_HI, clamped. Half/double-time is not corrected."""
    return min(1.0, max(0.0, (float(bpm) - _BPM_LO) / (_BPM_HI - _BPM_LO)))


def _raw(features) -> float | None:
    """Composite arc energy from the audio features present, or None if a track carries none of the
    three arc features. Weights are re-normalized over what's present, so a track with only BPM
    scores its normalized BPM, only energy scores its energy, etc."""
    if not features:
        return None
    num = den = 0.0
    for name, weight in _WEIGHTS.items():
        val = features.get(name)
        if val is None:
            continue
        val = _norm_bpm(val) if name == "bpm" else float(val)
        num += weight * val
        den += weight
    return num / den if den else None


def _mean(values) -> float | None:
    return sum(values) / len(values) if values else None


def arc_energies(keys, genres, audio) -> dict:
    """Per-track arc energy in [0,1] for every key.

    keys:   iterable of identity keys (the candidate pool being ordered).
    genres: {key: genre string} (missing/empty -> untagged).
    audio:  {key: {feature: value}} for the keys that carry audio metadata (others absent).

    Returns {key: arc_energy}. Tracks with features score their composite directly; the rest fall
    back through subgenre -> family pool averages -> the curated family constant -> 0.5.
    """
    keys = list(keys)
    raw = {k: _raw(audio.get(k)) for k in keys}

    by_subgenre, by_family = {}, {}
    for k in keys:
        if raw[k] is None:
            continue
        g = genres.get(k, "")
        sg = genre_map.subgenre(g)
        if sg is not None:
            by_subgenre.setdefault(sg, []).append(raw[k])
        by_family.setdefault(genre_map.family(g), []).append(raw[k])

    sub_avg = {sg: _mean(vs) for sg, vs in by_subgenre.items()}
    fam_avg = {f: _mean(vs) for f, vs in by_family.items()}

    out = {}
    for k in keys:
        if raw[k] is not None:
            out[k] = raw[k]
            continue
        g = genres.get(k, "")
        sg = genre_map.subgenre(g)
        val = sub_avg.get(sg) if sg is not None else None
        if val is None:
            val = fam_avg.get(genre_map.family(g))
        if val is None:
            val = genre_map.energy(g)   # curated family constant; untagged -> 0.5
        out[k] = val
    return out
