"""Walk existing taste clusters and rank only a bounded local neighbourhood.

The recommendation worker owns clustering, taste scoring and prepared pools.
Radio reads those results, never the library embedding matrix. Track vectors
are fetched by key for the selected clusters, without rebuilding any model.
"""
import hashlib
from itertools import zip_longest
import random

import numpy as np

from yt_playlist.rec import embed, rec_params
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.util import genre_map

CLUSTER_POOL_LIMIT = 96
NEIGHBOUR_COUNT = 4


def _unit(vector):
    norm = float(np.sqrt(np.einsum("i,i->", vector, vector, optimize=False)))
    return vector / norm if norm > 0 else vector


def _cosine(a, b):
    return float(np.einsum("i,i->", a, b, optimize=False))


class RadioCandidates:
    @work_span("radio: load prepared cluster pools")
    def __init__(self, store, now):
        self.store = store
        self.rng = random.Random()
        self.tracks, self.index, self.by_video = [], {}, {}
        self.by_key, self.membership, self.pools, self.prior = {}, {}, {}, {}
        self.loaded, self.vectors, self.vector_reads, self.affinities = set(), {}, set(), {}
        self.maximum_length = 60 * rec_params.setting_param(store.settings, "max_track_minutes")
        self.blocked = store.rec_model.disliked_identity_keys() | store.rec_model.suppressed_keys("for_you", now)
        self.muted = {a.casefold() for a in store.rec_model.muted_artists()}
        # Read one generation of the existing model, not its full vector pools.
        with store._lock:
            self.generation = store.settings.get_setting("rec_content_generation")
            self.encoding_model = store.settings.get_setting("rec_content_model")
            space = embed.content_space_id(store)
            self.modes = {str(m["mode_id"]): m for m in store.modes.list_modes()
                          if m.get("space") == space}
            bundles = store.rec_surface.get_proposals("mode_bundles") or {}
        for mid in self.modes:
            bucket = bundles.get(mid, {})
            rows = [bucket.get(name, []) for name in ("wheelhouse", "explore", "comfort", "temporal")]
            pool, seen = [], set()
            for group in zip_longest(*rows):
                for item in group:
                    if not item or not item.get("video_id") or item["key"] in seen | self.blocked:
                        continue
                    if (item.get("artist") or "").casefold() in self.muted:
                        continue
                    track = {**item, "_radio_mode": mid}
                    seen.add(track["key"])
                    pool.append(track)
                    if len(pool) == CLUSTER_POOL_LIMIT:
                        break
                if len(pool) == CLUSTER_POOL_LIMIT:
                    break
            if pool:
                self.pools[mid] = pool
                self.retain(pool)
                self.membership.update({t["key"]: mid for t in pool})
                # Personal taste and collaborative rankings were already computed.
                ppr = {key: i for i, key in enumerate(bundles.get("_ppr", {}).get(mid, []))}
                self.prior[mid] = {t["key"]: .7 * (1 - i / len(pool)) +
                    (.3 / (1 + ppr[t["key"]]) if t["key"] in ppr else 0)
                    for i, t in enumerate(pool)}
        self.modes = {mid: m for mid, m in self.modes.items() if mid in self.pools}
        self.centroids = {mid: _unit(m["centroid"]) for mid, m in self.modes.items()}
        # This tiny graph contains saved cluster centroids, not all song pairs.
        self.neighbours = {mid: sorted((other for other in self.modes if other != mid),
            key=lambda other: _cosine(center, self.centroids[other]), reverse=True)[:NEIGHBOUR_COUNT]
            for mid, center in self.centroids.items()}
        if not self.pools:
            # Cold start stays bounded, without another scoring pass or size gate.
            rows = bundles.get("all", {}).get("wheelhouse", [])[:CLUSTER_POOL_LIMIT]
            if not rows:
                with store._lock:
                    rows = [dict(r) for r in store.conn.execute(
                        "SELECT identity_key AS key, video_id, title, artist, album, thumbnail, "
                        "duration_s AS duration, genre FROM playable_library_tracks "
                        "WHERE video_id IS NOT NULL AND video_id<>'' LIMIT ?", (CLUSTER_POOL_LIMIT,))]
            pool = [{**t, "_radio_mode": "local"} for t in rows if self._allowed(t)]
            self.pools["local"] = pool
            self.prior["local"] = {t["key"]: 1 - i / max(1, len(pool)) for i, t in enumerate(pool)}
            self.membership.update({t["key"]: "local" for t in pool})
            self.retain(pool)
            self.neighbours["local"] = []
        policy = [(mid, [(t["key"], t.get("pool_policy")) for t in pool]) for mid, pool in self.pools.items()]
        self.fingerprint = hashlib.sha256(repr(("local-radio-v1", self.generation, policy)).encode()).hexdigest()[:20]

    def _allowed(self, track):
        return (track.get("video_id") and track["key"] not in self.blocked
                and (track.get("artist") or "").casefold() not in self.muted
                and (not track.get("duration") or track["duration"] <= self.maximum_length))

    def retain(self, tracks):
        """Retain session metadata when historical songs leave the prepared pool."""
        for track in tracks:
            self.by_key.setdefault(track["key"], track)
            self.by_video.setdefault(track["video_id"], track)

    def _read_vectors(self, keys):
        keys = list(dict.fromkeys(k for k in keys if k not in self.vector_reads))
        if not keys:
            return
        self.vector_reads.update(keys)
        # Incremental placements retain these coordinates. Only a model refit changes them.
        with self.store._lock:
            if self.store.settings.get_setting("rec_content_model") != self.encoding_model:
                return
            for offset in range(0, len(keys), CLUSTER_POOL_LIMIT):
                batch = keys[offset:offset + CLUSTER_POOL_LIMIT]
                qs = ",".join("?" * len(batch))
                for row in self.store.conn.execute(
                        f"SELECT identity_key,vec FROM rec_content_vectors WHERE identity_key IN ({qs})", batch):
                    vector = np.frombuffer(row["vec"], dtype=np.float32)
                    if not self.centroids or len(vector) == len(next(iter(self.centroids.values()))):
                        self.vectors[row["identity_key"]] = _unit(vector)

    def _load(self, mid):
        if mid in self.loaded:
            return self.pools[mid]
        pool = self.pools.get(mid, [])
        keys = [t["key"] for t in pool]
        meta = self.store.rec_query.tracks_by_keys(keys)
        with self.store._lock:
            qs = ",".join("?" * len(keys))
            details = {r["key"]: dict(r) for r in self.store.conn.execute(
                "SELECT identity_key AS key, MIN(duration_s) AS duration, MIN(NULLIF(genre,'')) AS genre "
                f"FROM playable_library_tracks WHERE identity_key IN ({qs}) GROUP BY identity_key", keys)} if keys else {}
        valid = []
        for old in pool:
            key = old["key"]
            if key not in meta:
                continue
            track = {**old, **meta[key], **details.get(key, {})}
            if not self._allowed(track):
                continue
            self.by_key[key] = self.by_video[track["video_id"]] = track
            if key not in self.index:
                self.index[key] = len(self.tracks)
                self.tracks.append(track)
            valid.append(track)
        self.pools[mid] = valid
        self.loaded.add(mid)
        self._read_vectors([t["key"] for t in valid])
        return valid

    def cluster_for(self, track):
        key = track["key"]
        if key in self.membership:
            return self.membership[key]
        self._read_vectors([key])
        vector = self.vectors.get(key)
        if vector is not None and self.centroids:
            mid = max(self.centroids, key=lambda mid: _cosine(vector, self.centroids[mid]))
        else:
            family = genre_map.family(track.get("genre"))
            mid = max(self.pools, key=lambda mid: sum(n for f, n in self.modes.get(mid, {}).get("families", [])
                                                       if f == family))
        self.membership[key] = mid
        return mid

    def similarity(self, a, b):
        """One local pair, never one song against the library."""
        pair = tuple(sorted((a["key"], b["key"])))
        if pair in self.affinities:
            return self.affinities[pair]
        va, vb = self.vectors.get(a["key"]), self.vectors.get(b["key"])
        if va is not None and vb is not None:
            score = max(0., _cosine(va, vb))
        else:
            fa, fb = genre_map.family(a.get("genre")), genre_map.family(b.get("genre"))
            score = 1 - genre_map.family_distance(fa, fb) if fa and fb else .2
        self.affinities[pair] = score
        return score

    def _context(self, history):
        if not history:
            return next(iter(self.pools))
        recent = history[-8:]
        self._read_vectors([t["key"] for t in recent])
        center = None
        for age, track in enumerate(reversed(recent)):
            vector = self.vectors.get(track["key"])
            if vector is not None:
                weighted = .8 ** age * vector
                center = weighted if center is None else center + weighted
        if center is None or not self.centroids:
            return self.cluster_for(recent[-1])
        return max(self.centroids, key=lambda mid: _cosine(center, self.centroids[mid]))

    def _score(self, track, history, target, feedback):
        mid = self.cluster_for(track)
        score = .2 * self.prior.get(mid, {}).get(track["key"], 0)
        recent = history[-8:]
        if recent:
            weights = [.8 ** age for age in range(len(recent))]
            score += .3 * sum(w * self.similarity(track, anchor)
                for w, anchor in zip(weights, reversed(recent))) / sum(weights)
            score += .25 * self.similarity(track, recent[-1])
        if target:
            score += .35 * self.similarity(track, target)
        for key, strength in list((feedback or {}).items())[-24:]:
            anchor = self.by_key.get(key)
            if anchor:
                score += strength * max(0, self.similarity(track, anchor) - .45)
        return score

    def seed_offer(self, n=16):
        """Spread the art grid across prepared taste regions, without vectors."""
        modes = list(self.pools)
        self.rng.shuffle(modes)
        selected, used, artists = [], set(), set()
        while len(selected) < n:
            added = False
            for mid in modes:
                eligible = [t for t in self.pools[mid] if t["video_id"] not in used]
                if not eligible:
                    continue
                head = eligible[:16]
                track = max(head, key=lambda t: .4 * ((t.get("artist") or "") not in artists) +
                    .2 * self.prior[mid].get(t["key"], 0) + self.rng.random() * .1)
                selected.append(track)
                used.add(track["video_id"])
                artists.add(track.get("artist") or "")
                added = True
                if len(selected) == n:
                    break
            if not added:
                break
        return selected

    def next_track(self, history, *, excluded=(), target=None, feedback=None):
        current = self._context(history)
        destination = self.cluster_for(target) if target else current
        # Only current and destination pools enter track-level scoring.
        modes = list(dict.fromkeys((current, destination)))
        excluded = set(excluded) | {t["video_id"] for t in history}
        eligible = [t for mid in modes for t in self._load(mid) if t["video_id"] not in excluded]
        if not eligible:
            for neighbour in self.neighbours[current]:
                eligible = [t for t in self._load(neighbour) if t["video_id"] not in excluded]
                if eligible:
                    break
        if not eligible:
            return None
        artists = {(t.get("artist") or "").casefold() for t in history[-3:]}
        spaced = [t for t in eligible if (t.get("artist") or "").casefold() not in artists]
        return max(spaced or eligible, key=lambda t: self._score(t, history, target, feedback) + self.rng.random() * .02)

    def direction_anchor(self, history, other_anchors, *, excluded=(), feedback=None):
        current = self._context(history)
        taken = {self.cluster_for(t) for t in other_anchors}
        excluded = set(excluded) | {t["video_id"] for t in history}
        neighbours = self.neighbours[current]
        # Alternatives take distinct local edges, without a farthest-song search.
        destinations = [mid for mid in neighbours if mid not in taken]
        if not destinations:
            destinations = [mid for mid in [current, *neighbours] if mid not in taken]
        if not destinations:
            destinations = [current, *neighbours]
        for mid in destinations:
            eligible = [t for t in self._load(mid) if t["video_id"] not in excluded]
            if eligible:
                return max(eligible, key=lambda t: self._score(t, history, None, feedback))
        return None
