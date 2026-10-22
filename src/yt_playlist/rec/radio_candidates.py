"""Walk existing taste clusters and rank only a bounded local neighbourhood.

The recommendation worker owns clustering, taste scoring and prepared pools.
Track vectors are fetched by key for the selected clusters, without rebuilding
any model. The current-listening layer is sampled by a background worker and
cached; playback picks and status polls never load the library embedding matrix.
"""
import hashlib
from itertools import zip_longest
import logging
import random

import numpy as np

from yt_playlist.rec import arc_energy, embed, layers, rec_params, radio_tuning
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.util import genre_map

CLUSTER_POOL_LIMIT = 96
NEIGHBOUR_COUNT = 4
logger = logging.getLogger(__name__)


def _unit(vector):
    norm = float(np.sqrt(np.einsum("i,i->", vector, vector, optimize=False)))
    return vector / norm if norm > 0 else vector


def _cosine(a, b):
    return float(np.einsum("i,i->", a, b, optimize=False))


class RadioCandidates:
    @work_span("radio: load prepared cluster pools")
    def __init__(self, store, now, tuning=None):
        self.store = store
        self.tuning = radio_tuning.controls(tuning or radio_tuning.DEFAULTS)
        self.now_posterior, self.now_gain, self.now_sampled_at = {}, 0., now
        self.energies = {}
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
        self.refresh_now(now)
        for mid in self.modes:
            bucket = bundles.get(mid, {})
            names = ("wheelhouse", "explore", "comfort", "temporal", "fresh")
            rows = [[{**t, "_radio_fresh": name == "fresh", "_radio_comfort": name == "comfort"}
                     for t in bucket.get(name, [])] for name in names]
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
            # Retain a bounded mix of discovery and familiar candidates before
            # truncating. These are already prepared style pools, never a model fit.
            fresh = [t for t in pool if t.get("_radio_fresh")]
            familiar = [t for t in pool if not t.get("_radio_fresh")]
            quota = round(CLUSTER_POOL_LIMIT * .85 * (1 - self.tuning["comfort"]))
            reserve = fresh[:quota] + familiar[:CLUSTER_POOL_LIMIT - min(quota, len(fresh))]
            used = {t["key"] for t in reserve}
            pool = (reserve + [t for t in pool if t["key"] not in used])[:CLUSTER_POOL_LIMIT]
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
        self._neighbour_graph()
        if not self.pools:
            # Cold start stays bounded, without another scoring pass or size gate.
            rows = bundles.get("all", {}).get("wheelhouse", [])[:CLUSTER_POOL_LIMIT]
            if not rows:
                with store._lock:
                    rows = [dict(r) for r in store.conn.execute(
                        "SELECT identity_key AS key, video_id, title, artist, album, thumbnail, "
                        "duration_s AS duration, genre, mb_year AS year FROM playable_library_tracks "
                        "WHERE video_id IS NOT NULL AND video_id<>'' LIMIT ?", (CLUSTER_POOL_LIMIT,))]
            fresh = bundles.get("all", {}).get("fresh", []) or store.rec_surface.get_proposals("fresh_pool") or []
            fresh = [{**t, "_radio_fresh": True} for t in fresh if self._allowed(t)]
            quota = round(CLUSTER_POOL_LIMIT * .85 * (1 - self.tuning["comfort"]))
            combined = fresh[:quota] + rows + fresh[quota:]
            unique = {}
            for track in combined:
                if self._allowed(track):
                    unique.setdefault(track["video_id"], {**track, "_radio_mode": "local"})
            pool = list(unique.values())[:CLUSTER_POOL_LIMIT]
            self.pools["local"] = pool
            self.prior["local"] = {t["key"]: 1 - i / max(1, len(pool)) for i, t in enumerate(pool)}
            self.membership.update({t["key"]: "local" for t in pool})
            self.retain(pool)
            self.neighbours["local"] = []
        policy = [(mid, [(t["key"], t.get("pool_policy")) for t in pool]) for mid, pool in self.pools.items()]
        self.fingerprint = hashlib.sha256(repr(("local-radio-v2", self.generation,
            self.tuning, policy)).encode()).hexdigest()[:20]

    def refresh_now(self, now):
        """Called by preparation/maintenance workers, never by a playback pick or status."""
        self.now_sampled_at = now
        try:
            gain = rec_params.get_param(self.store, "now_gain")
            posterior = layers.now_mode_posterior(self.store, now) if gain > 0 and self.modes else None
            if self.store.settings.get_setting("rec_content_model") != self.encoding_model:
                posterior = None
            self.now_posterior = {str(mid): share for mid, share in (posterior or {}).items()
                                  if str(mid) in self.modes}
            self.now_gain = gain
            if hasattr(self, "centroids") and self.centroids:
                self._neighbour_graph()
        except Exception:
            self.now_posterior, self.now_gain = {}, 0.
            logger.debug("Radio current-listening sample deferred", exc_info=True)

    def _neighbour_graph(self):
        self.neighbours = {mid: sorted((other for other in self.modes if other != mid),
            key=lambda other: self._now(other, _cosine(center, self.centroids[other])),
            reverse=True)[:NEIGHBOUR_COUNT] for mid, center in self.centroids.items()}

    def configure(self, values):
        self.tuning = radio_tuning.controls(values)

    def _now(self, mid, base=0.):
        return radio_tuning.now_bonus(base, self.now_posterior.get(str(mid), 0), self.now_gain)

    def _allowed(self, track):
        return (track.get("video_id") and track["key"] not in self.blocked
                and track.get("available") != 0
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
        library = self.store.rec_query.tracks_by_keys(keys)
        videos = [t["video_id"] for t in pool]
        with self.store._lock:
            qs = ",".join("?" * len(videos))
            details = {r["video_id"]: dict(r) for r in self.store.conn.execute(
                "SELECT identity_key AS key, video_id, title, artist, album, thumbnail, "
                "duration_s AS duration, genre, mb_year AS year, available, bpm, energy, danceability "
                f"FROM catalog_tracks WHERE video_id IN ({qs})", videos)} if videos else {}
        valid = []
        for old in pool:
            key = old["key"]
            if old["video_id"] not in details:
                continue
            # Keep the prepared upload when multiple uploads share a recording.
            track = {**old, **details[old["video_id"]]}
            for metric in ("plays", "liked"):
                if metric in library.get(key, {}):
                    track[metric] = library[key][metric]
            if not self._allowed(track):
                continue
            self.by_key[key] = self.by_video[track["video_id"]] = track
            if key not in self.index:
                self.index[key] = len(self.tracks)
                self.tracks.append(track)
            valid.append(track)
        self.pools[mid] = valid
        self.loaded.add(mid)
        self._energy(valid)
        self._read_vectors([t["key"] for t in valid])
        return valid

    def _energy(self, tracks):
        self.energies.update(arc_energy.arc_energies(
            [t["key"] for t in tracks], {t["key"]: t.get("genre") for t in tracks},
            {t["key"]: {k: t.get(k) for k in ("bpm", "energy", "danceability")} for t in tracks}))

    def refresh_metadata(self, tracks):
        """Refresh only the visible menu's uploads after the shared worker enriches them."""
        videos = list(dict.fromkeys(t["video_id"] for t in tracks))[:32]
        if not videos:
            return False
        with self.store._lock:
            qs = ",".join("?" * len(videos))
            rows = self.store.conn.execute(
                "SELECT identity_key AS key, video_id, title, artist, thumbnail, genre, mb_year AS year, "
                f"available, duration_s AS duration, bpm, energy, danceability FROM catalog_tracks WHERE video_id IN ({qs})",
                videos).fetchall()
        changed = False
        by_video = {row["video_id"]: dict(row) for row in rows}
        for track in tracks:
            values = by_video.get(track["video_id"])
            if values and any(track.get(k) != v for k, v in values.items()):
                track.update(values)
                self.by_key[track["key"]] = self.by_video[track["video_id"]] = track
                changed = True
        if changed:
            self.affinities.clear()
            self._energy(tracks)
        return changed

    def fits(self, track, anchor=None):
        """Only known, clear mismatches invalidate a future song after enrichment."""
        if not self._allowed(track):
            return False
        energy = self.energies.get(track["key"], genre_map.energy(track.get("genre")))
        target = self.tuning["energy"]
        if track.get("genre") or any(track.get(k) is not None for k in ("bpm", "energy", "danceability")):
            if abs(target - .5) > .3 and abs(energy - target) > .6:
                return False
        if anchor and self.tuning["theme"] > .8:
            a, b = genre_map.family(anchor.get("genre")), genre_map.family(track.get("genre"))
            if a and b and genre_map.family_distance(a, b) > .45:
                return False
        return True

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
            score += (.2 + .9 * self.tuning["theme"]) * self.similarity(track, target)
        comfort = self.tuning["comfort"]
        familiarity = min(1., (track.get("plays") or 0) / 10 + .5 * bool(track.get("liked"))
                          + .3 * bool(track.get("_radio_comfort")))
        score += .7 * comfort * familiarity
        energy = self.energies.get(track["key"], genre_map.energy(track.get("genre")))
        score -= 1.4 * abs(self.tuning["energy"] - .5) * abs(energy - self.tuning["energy"])
        if recent and self.tuning["theme"] < .5:
            a, b = genre_map.family(track.get("genre")), genre_map.family(recent[-1].get("genre"))
            distance = genre_map.family_distance(a, b) if a and b else 0
            score += (1 - 2 * self.tuning["theme"]) * (.5 if 0 < distance <= .5 else 0)
        for key, strength in list((feedback or {}).items())[-24:]:
            anchor = self.by_key.get(key)
            if anchor:
                score += strength * max(0, self.similarity(track, anchor) - .45)
        return self._now(mid, score)

    def _choose(self, eligible, history, target, feedback):
        if not eligible:
            return None
        fit = [t for t in eligible if self.fits(t, target)]
        eligible = fit or eligible  # Thin pools soften preferences, never eligibility.
        artists = {(t.get("artist") or "").casefold() for t in history[-3:]}
        spaced = [t for t in eligible if (t.get("artist") or "").casefold() not in artists]
        eligible = spaced or eligible
        recent = history[-5:]
        share = .85 * (1 - self.tuning["comfort"])
        discovery = share * (len(recent) + 1) - sum(bool(t.get("_radio_fresh")) for t in recent) >= .5
        source = [t for t in eligible if bool(t.get("_radio_fresh")) == discovery]
        return max(source or eligible, key=lambda t: self._score(t, history, target, feedback) + self.rng.random() * .02)

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
        modes = [destination] if self.tuning["theme"] > .8 else list(dict.fromkeys((current, destination)))
        if self.tuning["theme"] < .35:
            modes += sorted(self.neighbours[current], key=lambda mid: self._now(mid), reverse=True)[:2]
        modes = list(dict.fromkeys(modes))
        excluded = set(excluded) | {t["video_id"] for t in history}
        eligible = [t for mid in modes for t in self._load(mid) if t["video_id"] not in excluded]
        if not eligible:
            for neighbour in self.neighbours[current]:
                eligible = [t for t in self._load(neighbour) if t["video_id"] not in excluded]
                if eligible:
                    break
        if not eligible:
            return None
        return self._choose(eligible, history, target, feedback)

    def direction_anchor(self, history, other_anchors, *, excluded=(), feedback=None):
        current = self._context(history)
        taken = {self.cluster_for(t) for t in other_anchors}
        excluded = set(excluded) | {t["video_id"] for t in history}
        # Apply the categorical preference before choosing/truncating local styles.
        neighbours = sorted(self.neighbours[current], key=lambda mid: self._now(mid), reverse=True)
        # Alternatives take distinct local edges, without a farthest-song search.
        destinations = [mid for mid in neighbours if mid not in taken]
        if not destinations:
            destinations = [mid for mid in [current, *neighbours] if mid not in taken]
        if not destinations:
            destinations = [current, *neighbours]
        for mid in destinations:
            eligible = [t for t in self._load(mid) if t["video_id"] not in excluded]
            if eligible:
                return self._choose(eligible, history, history[-1] if history and self.tuning["theme"] > .8 else None, feedback)
        return None
