"""Score a Road Trip snapshot before applying duration and share constraints."""
import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from yt_playlist.core.activity import check_stopping
from yt_playlist.rec.road_trip_artists import selected_artist
from yt_playlist.rec.vector_math import percentile_scores

FAMILIARITY_SIGMA = 0.3
REPEAT_PENALTY = 0.3
CROSSOVER_SIMILARITY = 0.8
SEEDS_PER_INPUT = 4


@dataclass(frozen=True)
class TrackRanking:
    passenger_fit: dict[str, float] = field(default_factory=dict)
    driver_fit: dict[str, float] = field(default_factory=dict)

    def annotate(self, candidate):
        similarity = self.passenger_fit.get(candidate["video_id"])
        return {**candidate, "passenger_similarity": similarity} if similarity is not None else candidate


def _seed_rows(own, passengers, artists, genres, keys, index, artist_refs):
    genres = {name.strip().casefold() for name in genres if name}
    used = Counter()
    seeds = set()
    # Direct passenger results are already sorted by provider popularity.
    # Credited recordings can supply evidence before provider acquisition.
    for rows in (passengers, reversed(own)):
        for row in rows:
            genre = (row.get("genre_input") or row.get("genre") or "").strip().casefold()
            if artist := selected_artist(row, artists, artist_refs, include_origin=True):
                label = ("artist", artist)
            elif row["source"] == "theirs" and genre in genres:
                label = ("genre", genre)
            else:
                continue
            vector = index.get(keys.get(row["video_id"]))
            if vector is not None and vector not in seeds and used[label] < SEEDS_PER_INPUT:
                seeds.add(vector)
                used[label] += 1
    return sorted(seeds)


def rank_tracks(own, passengers, artists, genres, keys, content, now_scores, *, artist_refs=None):
    """Use published recording evidence where present and keep unknowns neutral."""
    scores = {}
    for row in own:
        video = row["video_id"]
        score = now_scores.get(keys.get(video))
        if score is not None and math.isfinite(score):
            scores[video] = score
    ranks = percentile_scores(scores)
    # Center ranks so a tied pool contributes zero, including a single known track.
    midpoint = (len(ranks) + 1) / (2 * len(ranks)) if ranks else .5
    driver_fit = {video: rank - midpoint for video, rank in ranks.items()}
    _, matrix, index = content
    seeds = _seed_rows(own, passengers, artists, genres, keys, index, artist_refs or {})
    passenger_fit = {}
    if seeds:
        seed_vectors = matrix[seeds].T
        videos = [row["video_id"] for row in [*own, *passengers]]
        for start in range(0, len(videos), 512):
            check_stopping()
            modeled = [(video, idx) for video in videos[start:start + 512]
                       if (idx := index.get(keys.get(video))) is not None]
            if modeled:
                similarities = np.max(matrix[[idx for _, idx in modeled]] @ seed_vectors, axis=1)
                passenger_fit.update((video, float(np.clip(value, 0, 1)))
                                     for (video, _), value in zip(modeled, similarities, strict=True))
    return TrackRanking(passenger_fit, driver_fit)


def sample_order(candidates, rng, familiarity, penalized, ranking):
    """Weighted draws retain familiarity and repeat avoidance on both trip sides."""
    keyed = []
    for candidate in candidates:
        video = candidate["video_id"]
        weight = candidate["score"]
        weight *= math.exp(-((candidate["fam"] - familiarity) ** 2) / (2 * FAMILIARITY_SIGMA ** 2)) + .05
        weight *= 1 + ranking.passenger_fit.get(video, 0)
        if candidate["source"] == "mine":
            weight *= 1 + ranking.driver_fit.get(video, 0)
        if video in penalized:
            weight *= REPEAT_PENALTY
        if weight > 0:
            keyed.append((rng.random() ** (1.0 / weight), candidate))
    keyed.sort(key=lambda pair: -pair[0])
    return [candidate for _, candidate in keyed]
