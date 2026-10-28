"""Rank bounded lookahead, replenished through the listener's style clusters.

The recommendation worker owns clustering, taste scoring and prepared pools.
Track vectors are fetched by key for the selected clusters, without rebuilding
any model. The current-listening layer is sampled by a background worker and
cached; playback picks and status polls never load the library embedding matrix.
"""
import hashlib
from contextlib import nullcontext
from itertools import zip_longest
import logging
import random
import threading

import numpy as np

from yt_playlist.rec import (embedding_vectors as vectors, layers, musical_character, rec_params,
                             radio_catalog, radio_intensity, radio_tuning)
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.core import json_cache
from yt_playlist.rec.radio_genres import GenrePolicy, genre
from yt_playlist.util import genre_map

CLUSTER_POOL_LIMIT = 96
POLICY_VERSION = 'cluster-radio-v8-skip-blackouts'
NEIGHBOUR_COUNT = 4
GENRE_POOL_LIMIT = 4
ARTIST_GAP = 6
_UNASSIGNED = object()
logger = logging.getLogger(__name__)
_cache_lock = threading.Lock()


def _input_cache(store):
    with _cache_lock:
        if not hasattr(store, '_radio_input_cache'):
            store._radio_input_cache = json_cache.DecodeCache()
        return store._radio_input_cache


def _prepared_inputs(store, *, project=None):
    """Read one committed model/pool generation; inflate after closing its reader."""
    owned = (getattr(store, '_read_only', False)
             or getattr(store._lock, 'held_by_current_thread', False))
    with (nullcontext(store) if owned else store.read_view()) as reader:
        generation = reader.settings.get_setting('rec_content_generation')
        encoding_model = reader.settings.get_setting('rec_content_model')
        space = vectors.content_space_id(reader, snapshot={'model': encoding_model})
        modes = {str(mode['mode_id']): mode for mode in reader.modes.list_modes()
                 if mode.get('space') == space}
        encoded = reader.rec_proposals._proposal_json('mode_bundles')
    # Preserve every Radio pool and its fallback, without restoring unrelated
    # Home preset/genre copies of the same musical profiles.
    fields = {'all', '_meta', '_radio', '_ppr', *modes}
    select = (lambda value: project(value or {}, modes, space, generation)) if project else None
    decode = (json_cache.decode if getattr(store._lock, 'held_by_current_thread', False)
              else _input_cache(store).decode)
    bundles = decode(encoded, keys=fields, project=select) if encoded is not None else None
    return generation, encoding_model, space, modes, bundles or {}


def _initial_pool(bucket, mid, blocked, muted, comfort):
    """The prepared interleave and discovery quota, using only track metadata."""
    names = ("wheelhouse", "explore", "comfort", "temporal", "fresh")
    pool, seen = [], set()
    for group in zip_longest(*(bucket.get(name, []) for name in names)):
        for name, item in zip(names, group):
            if (not item or not item.get("video_id")
                    or item["key"] in seen or item["key"] in blocked):
                continue
            if (item.get("artist") or "").casefold() in muted:
                continue
            track = {**item, "_radio_fresh": name == "fresh",
                     "_radio_comfort": name == "comfort", "_radio_mode": mid}
            seen.add(track["key"])
            pool.append(track)
    fresh = [t for t in pool if t.get("_radio_fresh")]
    familiar = [t for t in pool if not t.get("_radio_fresh")]
    quota = round(CLUSTER_POOL_LIMIT * .85 * (1 - comfort))
    reserve = fresh[:quota] + familiar[:CLUSTER_POOL_LIMIT - min(quota, len(fresh))]
    used = {t["key"] for t in reserve}
    return (reserve + [t for t in pool if t["key"] not in used])[:CLUSTER_POOL_LIMIT]


def _initial_projection(bundles, modes, space, generation, blocked, muted, comfort):
    """Restore evidence only after choosing the existing bounded live lookahead."""
    result = {key: bundles[key] for key in ('_radio', '_ppr', '_meta') if key in bundles}
    catalog = bundles.get('_radio') or {}
    members = (catalog.get('members') or {}) if (
        catalog.get('space') == space and catalog.get('generation') == generation) else {}
    has_pool = False
    for mid in modes:
        pool = _initial_pool(bundles.get(mid, {}), mid, blocked, muted, comfort)
        result[mid] = {'_radio_pool': pool}
        has_pool = has_pool or bool(pool or members.get(mid))
    if not has_pool and 'all' in bundles:
        # Cold fallback uses different eligibility and de-duplication rules;
        # retain its complete inputs for that existing path.
        result['all'] = bundles['all']
    return result


def _unit(vector):
    norm = float(np.sqrt(np.einsum("i,i->", vector, vector, optimize=False)))
    return vector / norm if norm > 0 else vector


def _cosine(a, b):
    return float(np.einsum("i,i->", a, b, optimize=False))


def _artist_gap(track, history):
    """Unknown artists do not collide; thin pools prefer the oldest repeat."""
    artist = " ".join((track.get("artist") or "").casefold().split())
    if artist:
        for gap, previous in enumerate(reversed(history[-ARTIST_GAP:]), 1):
            if artist == " ".join((previous.get("artist") or "").casefold().split()):
                return gap
    return ARTIST_GAP + 1


class RadioCandidates:
    @work_span("radio: load prepared cluster pools")
    def __init__(self, store, now, tuning=None, *, now_source=None, comparison=_UNASSIGNED):
        self.store = store
        self.prepared_at = now
        self.tuning = radio_tuning.controls(tuning or radio_tuning.DEFAULTS)
        self.now_posterior, self.now_gain, self.now_sampled_at = {}, 0., now
        self.energies = {}
        self.rng = random.Random()
        self.tracks, self.index, self.by_video = [], {}, {}
        self.by_key, self.membership, self.pools, self.prior = {}, {}, {}, {}
        self.loaded, self.vectors, self.vector_reads, self.affinities = set(), {}, set(), {}
        self.catalog_pools = {}
        self.intensity_pools = {}
        self.maximum_length = 60 * rec_params.setting_param(store.settings, "max_track_minutes")
        self.eligibility_at = now
        self.refresh_exclusions(now)
        self.genre_weights = store.rec_model.get_weights(now=now)
        # Read one generation of the existing model, not its full vector pools.
        self.generation, self.encoding_model, space, self.modes, bundles = _prepared_inputs(
            store, project=lambda value, modes, space, generation: _initial_projection(
                value, modes, space, generation, self.blocked, self.muted, self.tuning['comfort']))
        from yt_playlist.rec import product_comparison
        allow_assignment = comparison is _UNASSIGNED or bool(comparison)
        if comparison is _UNASSIGNED:
            comparison = getattr(now_source, 'comparison', None) if now_source else None
            if now_source and not comparison:
                allow_assignment = False
        paired, self.comparison = (product_comparison.prepared(store, 'radio', str(now), now, existing=comparison)
                                  if allow_assignment else (None, None))
        self.choice_context = {}
        if paired and paired['encoding_model'] == self.encoding_model:
            bundles = paired['bundles']
            self.modes = {str(mode['mode_id']): mode for mode in paired['modes'] if mode.get('space') == space}
            self.choice_context = {**paired['context'], 'comparison': self.comparison}
        else:
            self.comparison = None
        catalog = bundles.get('_radio') or {}
        self.cluster_members = (catalog.get('members') or {}) if (
            catalog.get('space') == space and (catalog.get('generation') == self.generation or self.comparison)) else {}
        self.catalog_scores = catalog.get('scores') or {}
        intensity = catalog.get('intensity') or {}
        self.intensity_index = intensity.get('clusters', {}) if (
            self.cluster_members and intensity.get('version') == radio_intensity.VERSION
            and intensity.get('profile_version') == musical_character.VERSION) else {}
        if now_source is None:
            self.refresh_now(now)
        else:
            # Retuning changes the prepared source mix, not listening evidence.
            # Never reload the full library matrix on an interactive Apply.
            self.now_sampled_at = 0
            if (self.encoding_model == now_source.encoding_model
                    and 0 <= now - now_source.now_sampled_at < radio_tuning.NOW_REFRESH_SECONDS):
                self.now_posterior = {mid: share for mid, share in now_source.now_posterior.items()
                                      if mid in self.modes}
                self.now_gain = now_source.now_gain
                self.now_sampled_at = now_source.now_sampled_at
        for mid in self.modes:
            bucket = bundles.get(mid, {})
            pool = (bucket['_radio_pool'] if '_radio_pool' in bucket else
                    _initial_pool(bucket, mid, self.blocked, self.muted, self.tuning['comfort']))
            if pool or self.cluster_members.get(mid):
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
                        "SELECT identity_key AS key, video_id, title, artist, album, album_browse_id, thumbnail, "
                        "duration_s AS duration, genre, mb_year AS year FROM playable_library_tracks "
                        "WHERE video_id IS NOT NULL AND video_id<>'' LIMIT ?", (CLUSTER_POOL_LIMIT,))]
            fresh = bundles.get("all", {}).get("fresh", []) or store.rec_proposals.get_proposals("fresh_pool") or []
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
        self.fingerprint = hashlib.sha256(repr((POLICY_VERSION, self.generation,
            musical_character.VERSION, bundles.get('_meta', {}).get('musical_generation'),
            self.tuning, policy)).encode()).hexdigest()[:20]

    def _cluster_pool(self, mid, history, excluded, *, genres=None):
        """Keep a bounded live lookahead without imposing a session-length cap."""
        self.refresh_exclusions()
        pool = self._load(mid)
        used = {t['key'] for t in history}
        used.update(self.by_video[v]['key'] for v in excluded if v in self.by_video)
        eligible = [t for t in pool if t['video_id'] not in excluded
                    and t['key'] not in used and self._allowed(t)
                    and (genres is None or genres.allows(t))]
        if len(eligible) >= 24 or mid not in self.cluster_members:
            return eligible
        added = radio_catalog.cluster_pool(self.store, self.cluster_members[mid], excluded,
            used | self.blocked | {t['key'] for t in eligible},
            lambda t: self._allowed(t) and (genres is None or genres.allows(t)),
            limit=CLUSTER_POOL_LIMIT - len(eligible))
        for track in added:
            track['_radio_mode'] = mid
            self.membership[track['key']] = mid
        self.retain(added)
        self._read_vectors([t['key'] for t in added])
        self._energy(added)
        self.pools[mid] = [*eligible, *added]
        return self.pools[mid]

    def _style_modes(self, current):
        """Themed stays in its style; Eclectic explores other style regions."""
        if self.tuning['theme'] == 1:
            return [current]
        center = self.centroids.get(current)
        others = [mid for mid in self.pools if mid != current]
        if center is not None:
            others.sort(key=lambda mid: _cosine(center, self.centroids[mid]),
                        reverse=self.tuning['theme'] >= .5)
        if self.tuning['theme'] > 0:
            others = others[:NEIGHBOUR_COUNT]
            # Intermediate settings progressively change the chance of leaving
            # the current region, rather than exhausting it at every dial value.
            return ([current, *others] if self.rng.random() < self.tuning['theme']
                    else [*others, current])
        return [*others, current]

    def _intensity_candidates(self, history, excluded, genres, styles):
        """Compare prepared fit across styles before hydrating at most four pools."""
        self.refresh_exclusions()
        self.genre_weights = self.store.rec_model.get_weights(now=max(self.prepared_at, self.now_sampled_at))
        used = {t['key'] for t in history} | self.blocked
        used.update(self.by_video[v]['key'] for v in excluded if v in self.by_video)
        energy = self.tuning['energy']
        order = list(dict.fromkeys([*styles, *self.pools]))
        options = []
        for mid in order:
            groups = self.intensity_index.get(mid)
            if groups:
                for priority, labels in radio_intensity.ordered_groups(groups, energy):
                    if any(self._genre_allowed(label) and genres.allows({'genre': label})
                           and any(key not in used for key in keys) for label, keys in labels.items()):
                        options.append((priority, order.index(mid), mid))
                        break
            else:
                rows = [t for t in self.pools[mid] if t['key'] not in used and t['video_id'] not in excluded
                        and self._allowed(t) and genres.allows(t)]
                if rows:
                    priority = min(radio_intensity.loss(musical_character.intensity(t), energy) for t in rows)
                    options.append((priority, order.index(mid), mid))
                elif self.cluster_members.get(mid):
                    options.append((radio_intensity.loss(.5, energy), order.index(mid), mid))
        eligible = []
        for _, _, mid in sorted(options)[:GENRE_POOL_LIMIT]:
            groups = self.intensity_index.get(mid)
            if groups:
                token = (mid, energy)
                pool = [t for t in self.intensity_pools.get(token, []) if t['key'] not in used
                        and t['video_id'] not in excluded and self._allowed(t) and genres.allows(t)]
                if len(pool) < 24:
                    keys = (key for _, labels in radio_intensity.ordered_groups(groups, energy)
                            for label, members in labels.items() if self._genre_allowed(label)
                            and genres.allows({'genre': label}) for key in members)
                    pool = radio_catalog.cluster_pool(self.store, keys, excluded, used,
                        lambda t: self._allowed(t) and genres.allows(t), limit=CLUSTER_POOL_LIMIT)
                    for track in pool:
                        track['_radio_mode'] = mid
                        self.membership[track['key']] = mid
                    self.retain(pool)
                    self._read_vectors([t['key'] for t in pool])
                    self.intensity_pools[token] = pool
            else:
                pool = (self._cluster_pool(mid, history, excluded, genres=genres)
                        if self.cluster_members else self._load(mid))
            eligible += [t for t in pool if t['video_id'] not in excluded and t['key'] not in used
                         and self._allowed(t) and genres.allows(t)]
        eligible = radio_intensity.narrow(eligible, energy)
        if self.cluster_members and eligible:
            # Keep an existing style whenever it offers the same fit tier.
            mid = min((self.cluster_for(t) for t in eligible), key=order.index)
            eligible = [t for t in eligible if self.cluster_for(t) == mid]
        return eligible

    def refresh_now(self, now):
        """Called by preparation/maintenance workers, never by a playback pick or status."""
        self.apply_now(self.sample_now(now))

    def sample_now(self, now, *, store=None):
        """Calculate privately; active sessions publish under their operation lock."""
        store = store or self.store
        try:
            gain = rec_params.get_param(store, "now_gain")
            posterior = layers.now_mode_posterior(store, now) if gain > 0 and self.modes else None
            if store.settings.get_setting("rec_content_model") != self.encoding_model:
                posterior = None
            return ({str(mid): share for mid, share in (posterior or {}).items()
                     if str(mid) in self.modes}, gain, now)
        except Exception:
            logger.debug("Radio current-listening sample deferred", exc_info=True)
            return {}, 0., now

    def apply_now(self, sample):
        self.now_posterior, self.now_gain, self.now_sampled_at = sample
        if getattr(self, "centroids", None):
            self._neighbour_graph()

    def _neighbour_graph(self):
        self.neighbours = {mid: sorted((other for other in self.modes if other != mid),
            key=lambda other: self._now(other, _cosine(center, self.centroids[other])),
            reverse=True)[:NEIGHBOUR_COUNT] for mid, center in self.centroids.items()}

    def configure(self, values):
        self.tuning = radio_tuning.controls(values)

    def refresh_exclusions(self, now=None):
        """Use the live feedback clock even when prepared/current-layer data is older."""
        self.eligibility_at = max(self.eligibility_at, self.prepared_at, self.now_sampled_at,
                                  now if now is not None else self.eligibility_at)
        self.blocked = (self.store.rec_model.disliked_identity_keys()
                        | self.store.rec_model.suppressed_keys('for_you', self.eligibility_at))
        self.muted = {a.casefold() for a in self.store.rec_model.muted_artists()}

    def _now(self, mid, base=0.):
        return radio_tuning.now_bonus(base, self.now_posterior.get(str(mid), 0), self.now_gain)

    def _allowed(self, track):
        return (track.get("video_id") and track["key"] not in self.blocked
                and track.get("available") != 0
                and (track.get("artist") or "").casefold() not in self.muted
                and self._genre_allowed(track.get('genre'))
                and (not track.get("duration") or track["duration"] <= self.maximum_length))

    def _genre_allowed(self, label):
        return (self.genre_weights.get(f'genre:{genre_map.family(label)}', 1) != 0
                and self.genre_weights.get(f'genre:{genre_map.subgenre(label)}', 1) != 0)

    def retain(self, tracks):
        """Retain session metadata when historical songs leave the prepared pool."""
        tracks = list(tracks)
        # Legacy saved sessions have no profile stamp. Hydrate those bounded
        # metadata records, while preserving profiles on already issued choices.
        self._energy([track for track in tracks if 'musical' not in track])
        for track in tracks:
            self.energies[track['key']] = musical_character.intensity(track)
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
                        f"SELECT identity_key,vec FROM rec_content_vectors WHERE identity_key IN ({qs}) "
                        f"UNION ALL SELECT identity_key,vec FROM rec_discovered_content_vectors WHERE identity_key IN ({qs})",
                        [*batch, *batch]):
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
        identity = next((i for i in self.store.identities.get_identities() if i.is_master), None)
        alternates = self.store.tracks.playback_alternates(identity.id, videos) if identity else {}
        with self.store._lock:
            qs = ",".join("?" * len(videos))
            details = {r["video_id"]: dict(r) for r in self.store.conn.execute(
                "SELECT identity_key AS key, video_id, title, artist, album, album_browse_id, thumbnail, "
                "duration_s AS duration, genre, mb_year AS year, available, bpm, energy, danceability "
                f"FROM catalog_tracks WHERE video_id IN ({qs})", videos)} if videos else {}
        valid = []
        for old in pool:
            key = old["key"]
            if old["video_id"] not in details:
                continue
            # Prefer a provider-witnessed playable upload over the old ID that
            # Music substitutes. Other recordings' uploads remain independent.
            track = {**old, **(alternates.get(old['video_id']) or details[old["video_id"]])}
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
        profiles = musical_character.load(self.store, [t['key'] for t in tracks])
        changed = False
        for track in tracks:
            profile = profiles.get(track['key'])
            changed |= track.get('musical') != profile
            track['musical'] = profile
            self.energies[track['key']] = musical_character.intensity(track)
        return changed

    def refresh_metadata(self, tracks):
        """Refresh only the visible menu's uploads after the shared worker enriches them."""
        videos = list(dict.fromkeys(t["video_id"] for t in tracks))[:32]
        if not videos:
            return False
        with self.store._lock:
            qs = ",".join("?" * len(videos))
            rows = self.store.conn.execute(
                "SELECT identity_key AS key, video_id, title, artist, album, album_browse_id, thumbnail, genre, mb_year AS year, "
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
        changed |= self._energy([track for track in tracks if track['video_id'] in videos])
        if changed:
            self.affinities.clear()
        return changed

    def fits(self, track, anchor=None):
        """Only known, clear mismatches invalidate a future song after enrichment."""
        if not self._allowed(track):
            return False
        if self.tuning["theme"] == 1 and not GenrePolicy(1, [], current=anchor).allows(track):
            return False
        energy, support = musical_character.intensity_evidence(track)
        target = self.tuning["energy"]
        # Genre/artist priors and legacy selected fields cannot reject a song.
        if energy is not None and support >= .4:
            if abs(target - .5) > .3 and abs(energy - target) > .6:
                return False
        if self.tuning["theme"] == 1 and anchor and self.cluster_members and target == .5:
            return self.cluster_for(track) == self.cluster_for(anchor)
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
        if len(self.affinities) > 4096:
            self.affinities.pop(next(iter(self.affinities)))
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

    def _score(self, track, history, target, feedback, genres):
        mid = self.cluster_for(track)
        score = .2 * self.prior.get(mid, {}).get(track["key"], self.catalog_scores.get(track['key'], 0))
        recent = history[-8:]
        continuity = 1 - genres.breadth
        if recent and continuity:
            weights = [.8 ** age for age in range(len(recent))]
            score += continuity * .3 * sum(w * self.similarity(track, anchor)
                for w, anchor in zip(weights, reversed(recent))) / sum(weights)
            score += continuity * .25 * self.similarity(track, recent[-1])
        if target and continuity:
            score += continuity * (.2 + .9 * self.tuning["theme"]) * self.similarity(track, target)
        comfort = self.tuning["comfort"]
        familiarity = min(1., (track.get("plays") or 0) / 10 + .5 * bool(track.get("liked"))
                          + .3 * bool(track.get("_radio_comfort")))
        score += .7 * comfort * familiarity
        energy = musical_character.intensity(track)
        score -= 1.4 * abs(self.tuning["energy"] - .5) * abs(energy - self.tuning["energy"])
        score += genres.bonus(track)
        for key, strength in list((feedback or {}).items())[-24:]:
            anchor = self.by_key.get(key)
            if anchor:
                score += strength * max(0, self.similarity(track, anchor) - .45)
        return self._now(mid, score)

    def _choose(self, eligible, history, target, feedback, genres):
        eligible = [t for t in eligible if self._allowed(t) and genres.allows(t)]
        if not eligible:
            return None
        eligible = radio_intensity.narrow(eligible, self.tuning['energy'])
        if genres.breadth == 1:
            # At the endpoint, similarity or a favourite artist cannot buy back
            # the genre we are actively trying to leave.
            contrast = max(genres.contrast(t) for t in eligible)
            eligible = [t for t in eligible if genres.contrast(t) == contrast]
        fit = [t for t in eligible if self.fits(t, genres.current if genres.pinned else target)]
        eligible = fit or eligible  # Thin pools soften preferences, never eligibility.
        gaps = [_artist_gap(t, history) for t in eligible]
        longest = max(gaps)
        eligible = [t for t, gap in zip(eligible, gaps) if gap == longest]
        recent = history[-5:]
        share = .85 * (1 - self.tuning["comfort"])
        discovery = share * (len(recent) + 1) - sum(bool(t.get("_radio_fresh")) for t in recent) >= .5
        source = [t for t in eligible if bool(t.get("_radio_fresh")) == discovery]
        return max(source or eligible, key=lambda t: self._score(t, history, target, feedback, genres) + self.rng.random() * .02)

    def _genre_modes(self, genres, excluded):
        """Inspect prepared metadata globally; hydrate/score at most four pools.

        Nearby centroids can all share one genre. Endpoints must be able to reach
        a contrasting genre, or the pinned genre in another cluster, directly.
        """
        ranked = []
        for mid, pool in self.pools.items():
            rows = [t for t in pool if t["video_id"] not in excluded
                    and self._allowed(t) and genres.allows(t)]
            if not rows:
                continue
            fit = [t for t in rows if self.fits(t, genres.current)]
            priority = max(genres.contrast(t) for t in rows) if not genres.pinned else (0., 0., 0.)
            rank = (priority, bool(fit)) if genres.breadth == 1 else (bool(fit), priority)
            ranked.append((mid, (*rank, self._now(mid))))
        return [mid for mid, _ in sorted(ranked, key=lambda item: item[1], reverse=True)]

    def _genre_pick(self, history, target, feedback, genres, excluded):
        modes = self._genre_modes(genres, excluded)[:GENRE_POOL_LIMIT]
        eligible = [t for mid in modes for t in self._load(mid) if t["video_id"] not in excluded]
        pick = self._choose(eligible, history, target, feedback, genres)
        if pick is not None or not genres.pinned:
            return pick
        return self._catalog_pick(history, target, feedback, genres, excluded)

    def _catalog_pick(self, history, target, feedback, genres, excluded):
        # Temporary #286 mitigation. #141 owns continuous cluster-space streams;
        # the fully Themed endpoint must not run dry at a Home-card pool boundary.
        label = genre(genres.current)
        if not label:
            return None
        used = {t["key"] for t in history}
        used.update(self.by_video[v]["key"] for v in excluded if v in self.by_video)
        pool = self.catalog_pools.get(label, [])
        eligible = [t for t in pool if t["video_id"] not in excluded and t["key"] not in used
                    and self._allowed(t) and genres.allows(t)]
        if not eligible:
            # New catalog entries were not present when session feedback built
            # its video exclusions. Apply current global suppressions on refill.
            self.refresh_exclusions()
            pool = radio_catalog.themed_pool(self.store, genres, excluded, used, self._allowed,
                                            limit=CLUSTER_POOL_LIMIT)
            self.catalog_pools[label] = pool
            for track in pool:
                self.by_key[track["key"]] = self.by_video[track["video_id"]] = track
                if track["key"] not in self.index:
                    self.index[track["key"]] = len(self.tracks)
                    self.tracks.append(track)
            self._energy(pool)
            self._read_vectors([t["key"] for t in pool])
            eligible = pool
        return self._choose(eligible, history, target, feedback, genres)

    def seed_offer(self, n=16):
        """Spread the art grid across prepared regions with bounded sparse-pool hydration."""
        self.refresh_exclusions()
        modes = list(self.pools)
        self.rng.shuffle(modes)
        if self.cluster_members and not any(self.pools.values()):
            for mid in modes[:min(n, GENRE_POOL_LIMIT)]:
                self._cluster_pool(mid, [], set())
        selected, used, artists = [], set(), set()
        while len(selected) < n:
            added = False
            for mid in modes:
                eligible = [t for t in self.pools[mid] if t["video_id"] not in used and self._allowed(t)]
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

    def next_track(self, history, *, excluded=(), target=None, feedback=None, current=None):
        if self.tuning['energy'] != .5:
            excluded = set(excluded) | {t['video_id'] for t in history}
            context = self.cluster_for(target or current or history[-1]) if (target or current or history) else next(iter(self.pools))
            genres = GenrePolicy(self.tuning['theme'], history, current=current)
            styles = (self._genre_modes(genres, excluded) if not self.cluster_members and (genres.pinned or genres.breadth == 1)
                      else self._style_modes(context))
            eligible = self._intensity_candidates(history, excluded, genres, styles)
            return self._choose(eligible, history, target, feedback, genres)
        if self.cluster_members:
            excluded = set(excluded) | {t['video_id'] for t in history}
            observed = current or (history[-1] if history else None)
            destination = observed if self.tuning['theme'] == 1 else target or observed
            context = self.cluster_for(destination) if destination else next(iter(self.pools))
            # Prepared styles guide continuity; the fully Themed endpoint still
            # requires the observed now-playing genre inside that style.
            genres = GenrePolicy(self.tuning['theme'], history, current=current)
            for mid in self._style_modes(context):
                eligible = self._cluster_pool(mid, history, excluded, genres=genres)
                if eligible:
                    return self._choose(eligible, history, target, feedback, genres)
            return None
        genres = GenrePolicy(self.tuning["theme"], history, current=current)
        excluded = set(excluded) | {t["video_id"] for t in history}
        if genres.pinned or genres.breadth == 1:
            return self._genre_pick(history, target, feedback, genres, excluded)
        context = self._context(history)
        destination = self.cluster_for(target) if target else context
        # The middle follows local styles; the eclectic side adds genre contrast.
        modes = [destination] if self.tuning["theme"] > .8 else list(dict.fromkeys((context, destination)))
        if genres.breadth:
            modes += [mid for mid in self._genre_modes(genres, excluded) if mid not in modes][:2]
        modes = list(dict.fromkeys(modes))
        eligible = [t for mid in modes for t in self._load(mid) if t["video_id"] not in excluded]
        # A busy local cluster is not evidence that the library has run out of
        # artists. Inspect only its bounded prepared neighbours before relaxing.
        for neighbour in self.neighbours[context]:
            if any(_artist_gap(t, history) > ARTIST_GAP and genres.allows(t)
                   and self.fits(t, target) for t in eligible):
                break
            if neighbour not in modes:
                eligible += [t for t in self._load(neighbour) if t["video_id"] not in excluded]
        if not eligible:
            return None
        return self._choose(eligible, history, target, feedback, genres)

    def direction_anchor(self, history, other_anchors, *, excluded=(), feedback=None):
        if self.tuning['energy'] != .5:
            excluded = set(excluded) | {t['video_id'] for t in [*history, *other_anchors]}
            current = self._context(history)
            genres = GenrePolicy(self.tuning['theme'], history + other_anchors,
                                 current=history[-1] if history else None)
            styles = (self._genre_modes(genres, excluded) if not self.cluster_members and (genres.pinned or genres.breadth == 1)
                      else self._style_modes(current))
            taken = {self.cluster_for(track) for track in other_anchors}
            if self.tuning['theme'] < 1:
                styles.sort(key=lambda mid: mid in taken)
            eligible = self._intensity_candidates(history, excluded, genres, styles)
            return self._choose(eligible, history, None, feedback, genres)
        if self.cluster_members:
            excluded = set(excluded) | {t['video_id'] for t in [*history, *other_anchors]}
            current = (self.cluster_for(history[-1]) if self.tuning['theme'] == 1 and history
                       else self._context(history))
            modes = self._style_modes(current)
            if self.tuning['theme'] < 1:
                taken = {self.cluster_for(track) for track in other_anchors}
                modes.sort(key=lambda mid: mid in taken)
            genres = GenrePolicy(self.tuning['theme'], history)
            for mid in modes:
                eligible = self._cluster_pool(mid, history, excluded, genres=genres)
                if eligible:
                    # Distinct positions inside one cluster remain available even
                    # when all four streams share that cluster at fully Themed.
                    self._read_vectors([t['key'] for t in other_anchors])
                    return max(eligible, key=lambda track: self._score(track, history, None, feedback, genres)
                        - .8 * max((self.similarity(track, anchor) for anchor in other_anchors), default=0))
            return None
        genres = GenrePolicy(self.tuning["theme"], history + other_anchors,
                             current=history[-1] if history else None)
        excluded = set(excluded) | {t["video_id"] for t in history}
        if genres.pinned or genres.breadth == 1:
            return self._genre_pick(history, None, feedback, genres, excluded)
        current = self._context(history)
        taken = {self.cluster_for(t) for t in other_anchors}
        # Apply the categorical preference before choosing/truncating local styles.
        neighbours = sorted(self.neighbours[current], key=lambda mid: self._now(mid), reverse=True)
        # Alternatives take distinct local edges, without a farthest-song search.
        destinations = [mid for mid in neighbours if mid not in taken]
        if not destinations:
            destinations = [mid for mid in [current, *neighbours] if mid not in taken]
        if not destinations:
            destinations = [current, *neighbours]
        # Distinct style edges are preferred, but another branch's cluster
        # must not force a repeat of an artist that just played.
        destinations += [mid for mid in [current, *neighbours] if mid not in destinations]
        eligible = []
        target = history[-1] if history and self.tuning["theme"] > .8 else None
        for mid in destinations:
            eligible += [t for t in self._load(mid) if t["video_id"] not in excluded]
            if any(_artist_gap(t, history) > ARTIST_GAP and self.fits(t, target) for t in eligible):
                break
        return self._choose(eligible, history, target, feedback, genres)
