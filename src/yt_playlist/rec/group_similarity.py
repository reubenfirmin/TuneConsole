"""Versioned, evidence-aware musical distance; independent of co-listening vectors.

The weights are hypotheses for shadow evaluation. Missing fields have no weight,
genre-derived priors never corroborate their own genre, and correlated character
classifiers share one bounded channel. No provider access or pool normalization.
"""
from dataclasses import dataclass
import hashlib

import numpy as np

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec import musical_profiles
from yt_playlist.util import genre_catalog, genre_map

POLICY = 'musical-neighbors-2/' + musical_profiles.VERSION
DIMENSIONS = musical_profiles.DIMENSIONS
CHANNELS = (('intensity', 'aggression', 'relaxed', 'density'), ('happiness', 'sadness'),
            ('groove',), ('instrumental',), ('acoustic',), ('tempo',))
AUDIO_ONLY_SUPPORT = 3.


def _independent(value):
    evidence = value.get('evidence', [])
    if value.get('kind') in ('curated', 'inherited'):
        return False
    # A mixed aggregate cannot be separated back into observations reliably.
    # Keep it unknown rather than count inherited genre traits as fresh audio.
    return all(_independent(item) for item in evidence)


@dataclass(frozen=True)
class Recording:
    key: str
    genre: str
    family: str
    year: int | None
    values: tuple
    support: tuple
    profile_version: str | None

    @property
    def known(self):
        return bool(self.genre or sum(max(self.support[DIMENSIONS.index(name)] for name in channel)
                                     for channel in CHANNELS) >= AUDIO_ONLY_SUPPORT)


def recording(key, features, profile=None):
    genre = genre_catalog.canonical_name(features.get('genre') or '')
    # Unrecognized provider tags are unknown, rather than arbitrary new musical islands.
    if genre_catalog.normalize(genre) not in genre_catalog.catalog()[1]:
        genre = ''
    dimensions = (profile or {}).get('dimensions', {}) if (profile or {}).get('version') == musical_profiles.VERSION else {}
    values, support = [], []
    for name in DIMENSIONS:
        dimension = dimensions.get(name, {})
        value = musical_profiles.number(dimension.get('value'))
        weight = musical_profiles.number(dimension.get('support')) or 0
        if value is None or not _independent(dimension):
            value, weight = 0., 0.
        if dimension.get('conflict'):
            weight *= .5
        values.append(value)
        support.append(weight)
    raw_year = str(features.get('year') or '')[:4]
    year = int(raw_year) if raw_year.isdigit() and 1900 <= int(raw_year) <= 2100 else None
    return Recording(key, genre, genre_map.family(genre) if genre else '', year,
                     tuple(values), tuple(support), (profile or {}).get('version'))


class Similarity:
    def __init__(self, recordings, *, style_weight=1., audio_cap=.8, era_penalty=0.):
        self.recordings = recordings
        self.style_weight, self.audio_cap, self.era_penalty = style_weight, audio_cap, era_penalty
        self.values = np.array([r.values for r in recordings], dtype=np.float32).reshape(-1, len(DIMENSIONS))
        self.support = np.array([r.support for r in recordings], dtype=np.float32).reshape(self.values.shape)
        self.genres = np.array([r.genre for r in recordings])
        self.families = np.array([r.family for r in recordings])
        self.years = np.array([r.year or 0 for r in recordings])

    def pairs(self, row, others):
        """One bounded batch against a row or aligned rows; return score and shared support."""
        others = np.asarray(others, dtype=int)
        shared = np.minimum(self.support[row], self.support[others])
        gap = np.abs(self.values[row] - self.values[others])
        tempo = DIMENSIONS.index('tempo')
        a, b = 30 + 270 * self.values[row, tempo], 30 + 270 * self.values[others, tempo]
        # 70 and 140 BPM can describe the same pulse. Do not claim a precise tempo distinction.
        ratio = np.abs(np.log2(a / b))
        gap[:, tempo] = np.minimum.reduce([ratio, np.abs(ratio - 1), np.abs(ratio - 2)])
        scores, weights = [], []
        for channel in CHANNELS:
            indices = [DIMENSIONS.index(name) for name in channel]
            w = shared[:, indices]
            total = w.sum(axis=1)
            scores.append(((1 - np.minimum(1, gap[:, indices] * 2)) * w).sum(axis=1)
                          / np.maximum(total, 1e-9))
            # One correlated channel cannot become four independent votes.
            weights.append(w.max(axis=1))
        weights = np.stack(weights, axis=1)
        audio_support = weights.sum(axis=1)
        audio = (np.stack(scores, axis=1) * weights).sum(axis=1) / np.maximum(audio_support, 1e-9)
        style_known = (self.genres[row] != '') & (self.genres[others] != '')
        style = np.where(self.genres[row] == self.genres[others], 1.,
                         np.where(self.families[row] == self.families[others], .68, .05))
        style_weight = style_known.astype(np.float32) * self.style_weight
        audio_weight = np.minimum(self.audio_cap, audio_support / 2)
        weight = style_weight + audio_weight
        score = (style * style_weight + audio * audio_weight) / np.maximum(weight, 1e-9)
        # Secondary era can refine a musical match, never establish one.
        dated = (self.years[row] != 0) & (self.years[others] != 0)
        score -= np.where(dated, np.minimum(1, np.abs(self.years[row] - self.years[others]) / 20) * self.era_penalty, 0)
        enough = style_known | (audio_support >= AUDIO_ONLY_SUPPORT)
        support = np.where(enough, np.maximum(style_known * .7, np.minimum(1, audio_support / 3)), 0)
        return np.clip(np.where(enough, score, 0), 0, 1), support

    def projection(self):
        """Only a proposal for recursive baselines; final split gates use the shared metric."""
        genres = sorted(set(self.genres) - {''})
        columns = np.zeros((len(self.recordings), len(genres) + len(DIMENSIONS)), dtype=np.float32)
        lookup = {name: i for i, name in enumerate(genres)}
        for i, r in enumerate(self.recordings):
            if r.genre:
                columns[i, lookup[r.genre]] = np.sqrt(self.style_weight)
        columns[:, len(genres):] = (self.values - .5) * np.sqrt(self.support) * .5 * np.sqrt(self.audio_cap / .8)
        norms = np.linalg.norm(columns, axis=1, keepdims=True)
        return columns / np.maximum(norms, 1e-9)


def _sample(bucket, row, limit, keys):
    """Keyed circular samples stay bounded even for a library with one dominant genre."""
    if len(bucket) <= limit:
        return bucket
    start = int.from_bytes(hashlib.sha256(keys[row].encode()).digest()[:8]) % len(bucket)
    stride = max(1, len(bucket) // limit)
    return [bucket[(start + i * stride) % len(bucket)] for i in range(limit)]


def graph(recordings, *, neighbors=16, candidates=128, minimum=.52, **weights):
    """Sparse candidate retrieval and exact supported edge weights; O(n * candidates) storage.

    Inverted style/family postings provide continuity; nearest supported scalar
    observations find cross-style audio candidates. Candidate retrieval is approximate
    and is explicitly measured by the offline harness, not claimed as exact k-NN.
    """
    metric = Similarity(recordings, **weights)
    keys = [r.key for r in recordings]
    postings = {}
    for i, r in enumerate(recordings):
        for token in (('style', r.genre), ('family', r.family)):
            if token[1]:
                postings.setdefault(token, []).append(i)
    ordered = []
    positions = []
    for dim in range(len(DIMENSIONS)):
        rows = np.flatnonzero(metric.support[:, dim] > 0)
        rows = rows[np.argsort(metric.values[rows, dim], kind='stable')]
        ordered.append(rows)
        positions.append({int(row): index for index, row in enumerate(rows)})
    edges = {}
    for row, r in enumerate(recordings):
        check_stopping()
        if not r.known:
            continue
        pool = set()
        for token in (('style', r.genre), ('family', r.family)):
            pool.update(_sample(postings.get(token, []), row, candidates // 3, keys))
        for dim in range(len(DIMENSIONS)):
            position = positions[dim].get(row)
            if position is not None:
                pool.update(map(int, ordered[dim][max(0, position - 5):position + 6]))
        pool.discard(row)
        others = sorted(pool)[:candidates]
        scores, support = metric.pairs(row, others)
        order = list(map(int, np.argsort(-scores, kind='stable')))
        # Keep a few supported cross-style links so a large style cannot hide
        # every weaker link to its broad musical area behind sixteen exact ties.
        cross = [i for i in order if recordings[others[i]].genre != r.genre][:max(1, neighbors // 4)]
        chosen = list(dict.fromkeys(order[:neighbors - len(cross)] + cross))
        for index in chosen:
            if scores[index] < minimum or support[index] == 0:
                continue
            other = others[int(index)]
            pair = tuple(sorted((row, other)))
            edges[pair] = (float(scores[index]), float(support[index]))
    return metric, [(a, b, weight, support) for (a, b), (weight, support) in sorted(edges.items())]
