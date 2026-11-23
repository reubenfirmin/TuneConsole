"""Rank bounded lookahead, replenished through the listener's musical groups.

The recommendation worker owns group discovery, taste scoring and prepared pools.
Track vectors are fetched by key for selected candidates without rebuilding
any model. The current-listening layer is sampled by a background worker and
cached; playback picks and status polls never load the library embedding matrix.
"""
from contextlib import nullcontext
from collections import defaultdict
from copy import copy, deepcopy
import hashlib
import logging
import math
import random

import numpy as np

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.work_diagnostics import work_span
from yt_playlist.rec import (embedding_vectors as vectors, group_learning, musical_character, rec_params,
                             radio_catalog, radio_intensity, radio_ranking, radio_rotation, radio_tuning, scoring)
from yt_playlist.rec.radio_candidate_inputs import prepared_inputs
from yt_playlist.rec.radio_genres import GenrePolicy
from yt_playlist.rec.radio_startup import allowed_seed, startup_shortlist
from yt_playlist.rec.stored_radio import initial_pool as _initial_pool, LOOKAHEAD as CLUSTER_POOL_LIMIT
from yt_playlist.util import genre_map

POLICY_VERSION = 'cluster-radio-v14'
NEIGHBOUR_COUNT = 4
GENRE_POOL_LIMIT = 4
logger = logging.getLogger(__name__)


def _bounded_track_groups(groups, current):
    """Keep the preferred order with room for the current group as a fallback."""
    bounded = groups[:GENRE_POOL_LIMIT]
    if current not in bounded and current in groups:
        bounded[-1] = current
    return bounded


def _unit(vector):
    norm = float(np.sqrt(np.einsum("i,i->", vector, vector, optimize=False)))
    return vector / norm if norm > 0 else vector


def _cosine(a, b):
    return float(np.einsum("i,i->", a, b, optimize=False))


class RadioCandidates:
    @work_span("radio: load prepared track group pools")
    def __init__(self, store, now, tuning=None, *, now_source=None, initial_queue=False):
        self.store = store
        self.prepared_at = now
        self.tuning = radio_tuning.controls(tuning or radio_tuning.DEFAULTS)
        self.now_posterior, self.now_gain, self.now_sampled_at, self.group_allocation = {}, 0., now, {}
        self.rng = random.Random()
        self.tracks, self.index, self.by_video = [], {}, {}
        self.by_key, self.membership, self.pools, self.prior = {}, {}, {}, {}
        self.loaded, self.vectors, self.vector_reads, self.affinities = set(), {}, set(), {}
        self.catalog_pools = {}
        self.intensity_pools = {}
        self.rotated = set()
        from yt_playlist.controllers.radio_inputs import inputs_for
        self.inputs = inputs_for(store)
        self.eligibility_at = now
        self.axes = {}
        self._scoped_axes = initial_queue
        self._tuning_read = False

        def capture(reader):
            if not self._scoped_axes:
                self.axes = self.inputs.taxonomy_facts(reader)
            self.maximum_length = 60 * rec_params.setting_param(reader.settings, 'max_track_minutes')
            self.blocked, self.muted = self.inputs.exclusions(reader, now)
            self.genre_weights = reader.rec_model.get_weights(now=now)
            return self.blocked, self.muted
        from yt_playlist.rec.stored_radio import PreparedRadio
        self.prepared = PreparedRadio(store, now)
        # Read one generation of the existing model, not its full vector pools.
        self.generation, self.encoding_model, _, self.modes, bundles = prepared_inputs(
            store, prepared=self.prepared, comfort=self.tuning['comfort'], capture=capture)
        self.group_head = bundles.get('_group_head')
        self.choice_context = {}
        if self.prepared.generation is not None:
            self.choice_context['recommendation_generations'] = [self.prepared.generation]
        catalog = bundles.get('_radio') or {}
        self.cluster_members = {version: members for version, members in (catalog.get('members') or {}).items()
                                if members}
        self.catalog_scores = catalog.get('scores') or {}
        intensity = catalog.get('intensity') or {}
        self.intensity_index = intensity.get('clusters', {}) if (
            self.cluster_members and intensity.get('version') == radio_intensity.VERSION
            and intensity.get('profile_version') == musical_character.VERSION) else {}
        self.group_generation = self.group_head['generation'] if self.group_head else None
        self.lazy_modes = bundles.get('_lazy_modes', set())
        if not self.lazy_modes or not self.cluster_members:
            self._load_taxonomy()
        if (now_source is not None and self.encoding_model == now_source.encoding_model
                and self.group_generation == getattr(now_source, 'group_generation', None)
                and 0 <= now - now_source.now_sampled_at < radio_tuning.NOW_REFRESH_SECONDS):
            self.now_posterior = {mid: share for mid, share in now_source.now_posterior.items() if mid in self.modes}
            self.now_gain = now_source.now_gain
            self.now_sampled_at = now_source.now_sampled_at
            self.group_allocation = {mid: share for mid, share in getattr(now_source, 'group_allocation', {}).items()
                                     if mid in self.modes}
        elif not self._scoped_axes and (now_source is None or self.group_head):
            self.refresh_now(now)
        else:
            self.now_sampled_at = 0
        retained = []
        for mid in self.modes:
            if mid in self.lazy_modes:
                self.pools[mid] = []
                continue
            if self.lazy_modes:
                continue
            bucket = bundles.get(mid, {})
            pool = (bucket['_radio_pool'] if '_radio_pool' in bucket else
                    _initial_pool(bucket, mid, self.blocked, self.muted, self.tuning['comfort']))
            if pool or self.cluster_members.get(mid):
                self.pools[mid] = pool
                retained.extend(pool)
                self.membership.update({t["key"]: mid for t in pool})
                # Personal taste and collaborative rankings were already computed.
                ppr = self.prepared.positions(mid, [t['key'] for t in pool])
                self._set_prior(mid, pool, ppr)
        self.retain(retained)
        self.modes = {mid: m for mid, m in self.modes.items() if mid in self.pools}
        self.centroids = {mid: _unit(m['centroid']) for mid, m in self.modes.items() if 'centroid' in m}
        self.group_paths = {}
        if self.group_head and self.modes:
            definitions = {group['version']: group['parent_version'] for group in self.group_head['groups']}
            for version in self.modes:
                path, cursor = [], version
                while cursor in definitions and cursor not in path:
                    path.append(cursor)
                    cursor = definitions[cursor]
                self.group_paths[version] = set(path)
            self.choice_context['group_generation'] = self.group_generation
            self.choice_context['musical_groups'] = [
                {key: group[key] for key in ('group_id', 'version', 'name')} for group in self.modes.values()]
        self._neighbour_graph()
        self.shared_fresh = (None if self.lazy_modes else
            bundles.get('all', {}).get('fresh', []) if self.group_head and self.pools else [])
        if not self.pools:
            # Cold start stays bounded, without another scoring pass or size gate.
            rows = bundles.get("all", {}).get("wheelhouse", [])[:CLUSTER_POOL_LIMIT]
            if not rows:
                rows = radio_catalog.fallback_pool(store, self._allowed, limit=CLUSTER_POOL_LIMIT)
            fresh = bundles.get("all", {}).get("fresh", [])
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
        policy = (tuple(self.pools) if self.lazy_modes else
                  [(mid, [(t["key"], t.get("pool_policy")) for t in pool]) for mid, pool in self.pools.items()])
        self._fingerprint_inputs = (POLICY_VERSION, self.generation,
            musical_character.VERSION, bundles.get('_meta', {}).get('musical_generation'),
            policy, self.prepared.generation)
        self._fingerprint()

    def _fingerprint(self):
        value = (*self._fingerprint_inputs[:4], self.tuning, *self._fingerprint_inputs[4:])
        self.fingerprint = hashlib.sha256(repr(value).encode()).hexdigest()[:20]

    def for_tuning(self, values):
        if not hasattr(self.prepared, 'for_tuning'):
            return None
        plan = copy(self)
        plan.configure(values)
        plan.prepared = self.prepared.for_tuning(plan.tuning['comfort'])
        plan.rng = random.Random()
        plan.rng.setstate(self.rng.getstate())
        plan.by_key, plan.by_video = dict(self.by_key), dict(self.by_video)
        plan.membership, plan.neighbours = dict(self.membership), dict(self.neighbours)
        plan.vectors, plan.vector_reads = dict(self.vectors), set(self.vector_reads)
        plan.affinities = dict(self.affinities)
        plan.tracks, plan.index = list(self.tracks), dict(self.index)
        plan.loaded, plan.rotated = set(), set()
        plan.catalog_pools, plan.intensity_pools = {}, {}
        plan.pools = {mode: [] if mode in self.lazy_modes else deepcopy(pool)
                      for mode, pool in self.pools.items()}
        plan.prior = {mode: scores for mode, scores in self.prior.items() if mode not in self.lazy_modes}
        plan._tuning_read = bool(self.lazy_modes and self.cluster_members)
        plan._scoped_axes = plan._tuning_read or self._scoped_axes
        plan.axes = {} if plan._tuning_read else dict(self.axes)
        plan._fingerprint()
        return plan

    def tuning_inputs(self, tracks, now):
        """Refresh mutable rules inside the menu's planning snapshot."""
        self.refresh_exclusions(now)
        self.maximum_length = self.prepared.planning_fact('radio-maximum-duration',
            lambda: 60 * rec_params.setting_param(self.read_store.settings, 'max_track_minutes'))
        self.genre_weights = self.prepared.planning_fact('radio-genre-weights',
            lambda: self.read_store.rec_model.get_weights(now=now))
        self._page_axes(self.read_store, tracks)

    def close(self):
        self.prepared.close()

    def _set_prior(self, mode, pool, ppr):
        self.prior[mode] = {track['key']: .7 * (1 - index / len(pool)) +
                            (.3 / (1 + ppr[track['key']]) if track['key'] in ppr else 0)
                            for index, track in enumerate(pool)}

    def _initial_track_group_pool(self, mode):
        if mode in self.lazy_modes and mode not in self.prior:
            with self.prepared.planning_read():
                if self._tuning_read and mode in self.cluster_members:
                    pool = self.prepared.tracks('radio', mode, 'membership', allowed=self._allowed,
                                                include_display=False, prepare_page=self._page_axes)
                    ppr = {}
                else:
                    pool = self.prepared.initial_track_group_pool(mode, self.blocked, self.muted, self.tuning['comfort'])
                    ppr = self.prepared.positions(mode, [track['key'] for track in pool])
                self.retain(pool)
            for track in pool:
                self.membership.setdefault(track['key'], mode)
            self.pools[mode] = pool
            self._set_prior(mode, pool, ppr)
        return self.pools.get(mode, [])

    @property
    def read_store(self):
        """Use the current planning snapshot without replacing the live owner."""
        prepared = getattr(self, 'prepared', None)
        return prepared.current_reader if prepared is not None and prepared.current_reader is not None else self.store

    def _cluster_pool(self, mid, history, excluded, *, genres=None):
        """Keep a bounded live lookahead without imposing a session-length cap."""
        self.refresh_exclusions()
        pool = self._load(mid)
        used = {t['key'] for t in history}
        used.update(self.by_video[v]['key'] for v in excluded if v in self.by_video)
        eligible = [t for t in pool if t['video_id'] not in excluded
                    and t['key'] not in used and self._allowed(t)
                    and (genres is None or genres.allows(t))]
        self._rotation_facts([*eligible, *history[-radio_rotation.WINDOW:]])
        wanted = radio_rotation.next_kind(history, self.tuning['comfort'])
        missing = not any(track['_radio_rotation'] == wanted for track in eligible)
        if self.group_head and self.tuning['energy'] == .5 and (wanted == 'new' and missing or not eligible):
            known = {track['key'] for track in eligible}
            fresh = [{**track, '_radio_mode': mid} for track in self._fresh_reserve(excluded, used, genres)
                     if track['key'] not in known and self.cluster_for(track) == mid]
            if fresh:
                self.retain(fresh)
                eligible = radio_rotation.reserve([*eligible, *fresh], CLUSTER_POOL_LIMIT, self.tuning['comfort'])
                missing = not any(track['_radio_rotation'] == wanted for track in eligible)
        if mid not in self.cluster_members or (
                mid in self.rotated and not missing and len(eligible) >= max(24, len(pool) * .75)):
            return eligible
        # Keep a taste-ranked reserve, and sample another bounded window of the
        # same prepared track group. A new session must reach beyond the Home head
        # even when that head alone could supply all four short branches.
        if mid not in self.rotated or missing:
            eligible = radio_rotation.reserve(eligible, CLUSTER_POOL_LIMIT // 2, self.tuning['comfort'])
            self.rotated.add(mid)
        rejected = used | self.blocked | {t['key'] for t in eligible}
        allowed = lambda t: self._allowed(t) and (genres is None or genres.allows(t))
        added = self.prepared.tracks('radio', mid, 'membership', excluded=excluded, used=rejected,
                                    allowed=allowed, limit=CLUSTER_POOL_LIMIT - len(eligible))
        for track in added:
            track['_radio_mode'] = mid
            self.membership[track['key']] = mid
        self.retain(added)
        self._read_vectors([t['key'] for t in added])
        self._energy(added)
        self.pools[mid] = [*eligible, *added]
        return self.pools[mid]

    def _fresh_reserve(self, excluded, used, genres):
        """Supplement owned leaves with ranked, currently eligible discoveries."""
        allowed = lambda track: self._allowed(track) and (genres is None or genres.allows(track))
        if self.shared_fresh is None:
            self.shared_fresh = self.prepared.tracks(self.prepared.fresh_surface, 'all', 'fresh',
                                                     allowed=self._allowed)
        tracks = [dict(track) for track in self.shared_fresh if track['key'] not in used
                  and track['video_id'] not in excluded and allowed(track)]
        self._rotation_facts(tracks)
        tracks = [track for track in tracks if track['_radio_rotation'] == 'new']
        if len(tracks) < CLUSTER_POOL_LIMIT // 2:
            used = used | {track['key'] for track in tracks}
            genre = genres.key(genres.current) if genres and genres.pinned else None

            def load():
                return self.prepared.tracks(self.prepared.fresh_surface, 'all', 'fresh',
                    excluded=excluded, used=used, allowed=allowed, limit=CLUSTER_POOL_LIMIT - len(tracks),
                    unheard_discoveries=True)

            key = ('fresh-reserve', genre, frozenset(excluded), frozenset(used))
            tracks += self.prepared.planning_fact(key, load)
        return [{**track, '_radio_fresh': True, '_radio_catalog_score': 1 - i / CLUSTER_POOL_LIMIT}
                for i, track in enumerate(tracks)]

    def _rotation_group_tracks(self, eligible, history, order, *, prefetched=False):
        """Prefer tracks from a nearby group supplying the due category within the same fit tier."""
        if not prefetched:
            self._rotation_facts([*eligible, *history[-radio_rotation.WINDOW:]])
        wanted = radio_rotation.next_kind(history, self.tuning['comfort'])
        source = [track for track in eligible if track['_radio_rotation'] == wanted] or eligible
        mid = min((self.cluster_for(track) for track in source), key=order.index)
        return [track for track in eligible if self.cluster_for(track) == mid]

    def _ordered_track_groups(self, current):
        """Themed starts inside its current leaf; its genre pin governs fallback."""
        others = [mid for mid in self.pools if mid != current]
        nearby = self.tuning['theme'] >= .5
        others.sort(key=lambda mid: self._track_group_priority(current, mid, nearby))
        if self.tuning['theme'] == 1:
            return [current, *others]
        if self.tuning['theme'] > 0:
            others = others[:NEIGHBOUR_COUNT]
            # Intermediate settings progressively change the chance of leaving
            # the current region, rather than exhausting it at every dial value.
            return ([current, *others] if self.rng.random() < self.tuning['theme']
                    else [*others, current])
        return [*others, current]

    def _intensity_group_order(self, current, genres, excluded):
        if self._tuning_read and self.prepared.has_intensity:
            return self._ordered_track_groups(current)
        if (genres.breadth == 1 and self.group_head
                or not self.cluster_members and (genres.pinned or genres.breadth == 1)):
            return self._genre_modes(genres, excluded)
        return self._ordered_track_groups(current)

    def _intensity_candidates(self, history, excluded, genres, group_order, preference, *, reserved=()):
        """Compare prepared fit across track groups before hydrating at most four pools."""
        if self.prepared.has_intensity:
            with self.prepared.planning_read():
                return self._stored_intensity_candidates(history, excluded, genres, group_order, preference,
                                                         reserved=reserved)
        self.refresh_exclusions()
        self.genre_weights = self.read_store.rec_model.get_weights(now=max(self.prepared_at, self.now_sampled_at))
        used = {t['key'] for t in history} | self.blocked
        used.update(self.by_video[v]['key'] for v in excluded if v in self.by_video)
        order = list(dict.fromkeys([*group_order, *self.pools]))
        options = []
        for mid in order:
            bands = self.intensity_index.get(mid)
            if bands:
                for priority, labels in radio_intensity.ordered_bands(bands, preference):
                    if any(self._genre_allowed(label) and genres.allows({'genre': label})
                           and any(key not in used for key in keys) for label, keys in labels.items()):
                        options.append((priority, order.index(mid), mid))
                        break
            else:
                rows = [t for t in self._initial_track_group_pool(mid)
                        if t['key'] not in used and t['video_id'] not in excluded and self._allowed(t) and genres.allows(t)]
                if rows:
                    priority = min(radio_intensity.priority(musical_character.intensity(t), preference) for t in rows)
                    options.append((priority, order.index(mid), mid))
                elif self.cluster_members.get(mid):
                    options.append((radio_intensity.priority(radio_intensity.NEUTRAL, preference),
                                    order.index(mid), mid))
        eligible = []
        for _, _, mid in sorted(options)[:GENRE_POOL_LIMIT]:
            bands = self.intensity_index.get(mid)
            if bands:
                token = (mid, preference)
                pool = [t for t in self.intensity_pools.get(token, []) if t['key'] not in used
                        and t['video_id'] not in excluded and self._allowed(t) and genres.allows(t)]
                if len(pool) < 24:
                    keys = (key for _, labels in radio_intensity.ordered_bands(bands, preference)
                            for label, members in labels.items() if self._genre_allowed(label)
                            and genres.allows({'genre': label}) for key in members)
                    pool = radio_catalog.cluster_pool(self.read_store, keys, excluded, used,
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
        return self._selection_tier(eligible, history, excluded, genres, used, order, preference, reserved)

    def _stored_intensity_candidates(self, history, excluded, genres, group_order, preference, *, heads=False, reserved=()):
        def eligibility():
            self.refresh_exclusions()
            return self.read_store.rec_model.get_weights(now=max(self.prepared_at, self.now_sampled_at))
        self.genre_weights = self.prepared.planning_fact('radio-genre-weights', eligibility)
        used = {t['key'] for t in history} | self.blocked
        used.update(self.by_video[video]['key'] for video in excluded if video in self.by_video)
        order = list(dict.fromkeys([*group_order, *self.pools]))
        if self._tuning_read and (genres.pinned or genres.breadth or genres.focus or self._has_genre_exclusions()):
            headings = self.prepared.intensity_headings()
            self._page_axes(self.read_store, ({'genre': label} for bands in headings.values()
                                             for labels in bands.values() for label in labels))
        allowed = lambda track: self._allowed(track) and genres.allows(track)
        genre_matches = {}

        def genre_allowed(label):
            if label not in genre_matches:
                genre_matches[label] = self._genre_allowed(label) and genres.allows({'genre': label})
            return genre_matches[label]

        criteria = dict(allowed=allowed, genre_allowed=genre_allowed, excluded=excluded, used=used)
        eclectic = self.group_head and genres.breadth == 1
        options = radio_intensity.rank_prepared_track_groups(
            self.prepared, order, preference, criteria, limit=GENRE_POOL_LIMIT, eclectic=eclectic,
            contrast=genres.contrast if genres.recent else None)
        eligible = []
        for _, _, mid, witness in options:
            if not self._tuning_read:
                self._initial_track_group_pool(mid)
            token = (mid, preference)
            pool = [track for track in self.intensity_pools.get(token, []) if track['key'] not in used
                    and track['video_id'] not in excluded and allowed(track)]
            if len(pool) < 24:
                _, pool = self.prepared.intensity_pool(mid, preference, **criteria,
                                                      limit=16 if heads else CLUSTER_POOL_LIMIT)
                if not pool:
                    pool = self.prepared.tracks('radio', mid, 'membership', allowed=allowed, excluded=excluded,
                                                used=used, include_display=False, prepare_page=self._page_axes,
                                                limit=16 if heads else CLUSTER_POOL_LIMIT)
                for track in pool:
                    track['_radio_mode'] = mid
                    self.membership[track['key']] = mid
                self.retain(pool)
                self._read_vectors([track['key'] for track in pool])
                self.intensity_pools[token] = pool
            if eclectic:
                # A rotating lookahead can miss the recording used to admit this leaf.
                witness['_radio_mode'] = mid
                self.membership[witness['key']] = mid
                self.retain([witness])
                pool = [witness, *(track for track in pool if track['key'] != witness['key'])][:CLUSTER_POOL_LIMIT]
            eligible.extend(pool)
        if heads:
            wanted = radio_rotation.next_kind(history, self.tuning['comfort'])
            if wanted == 'new' and not any(track['_radio_rotation'] == wanted for track in eligible):
                fresh = self._fresh_reserve(excluded, used | {track['key'] for track in eligible}, genres)
                self.retain(fresh)
                self._read_vectors([track['key'] for track in fresh])
                eligible.extend(fresh)
            return eligible
        return self._selection_tier(eligible, history, excluded, genres, used, order, preference, reserved)

    def _selection_tier(self, eligible, history, excluded, genres, used, order, preference, reserved):
        wanted = radio_rotation.next_kind(history, self.tuning['comfort'])
        if self.group_head and (wanted == 'new' or not eligible):
            fresh = self._fresh_reserve(excluded, used | {track['key'] for track in eligible}, genres)
            self.retain(fresh)
            eligible.extend(fresh)
        eligible = radio_intensity.narrow(eligible, preference)
        if reserved:
            eligible = [track for track in eligible if track['video_id'] not in reserved] or eligible
        # Eclectic compares contrast across groups in the selected energy tier.
        if self.cluster_members and eligible and not (self.group_head and genres.breadth == 1):
            return self._rotation_group_tracks(eligible, history, order)
        return eligible

    def refresh_now(self, now):
        """Called by preparation/maintenance workers, never by a playback pick or status."""
        self.apply_now(self.sample_now(now))

    def sample_now(self, now, *, store=None):
        """Calculate privately; active sessions publish under their operation lock."""
        store = store or self.store
        try:
            if self.group_head:
                prior = scoring.group_prior(store, self.group_head, now)
                posterior, gain = prior
                allocation = group_learning.current_allocation(store, self.group_head, now, prior=prior)
                return ({version: posterior[group['group_id']] for version, group in self.modes.items()
                         if group['group_id'] in posterior and gain},
                        gain if posterior else 0., now, {version: allocation.get(group['group_id'], 0.)
                                    for version, group in self.modes.items()})
            posterior, gain = scoring.mode_prior(store, now) if self.modes else (None, 0.)
            if store.rec_model.recording_encoder() != self.encoding_model:
                posterior = None
            return ({str(mid): share for mid, share in (posterior or {}).items()
                     if str(mid) in self.modes}, gain, now, {})
        except Exception:
            logger.debug("Radio current-listening sample deferred", exc_info=True)
            return {}, 0., now, {}

    def apply_now(self, sample):
        self.now_posterior, self.now_gain, self.now_sampled_at, self.group_allocation = sample
        if hasattr(self, 'group_paths'):
            self._neighbour_graph()

    def _neighbour_graph(self):
        self.neighbours = {}
        if not self.lazy_modes:
            for mode in self.modes:
                self._neighbours(mode)

    def _neighbours(self, mode):
        if mode not in self.neighbours:
            self.neighbours[mode] = sorted((other for other in self.modes if other != mode),
                key=lambda other: self._track_group_priority(mode, other, True))[:NEIGHBOUR_COUNT]
        return self.neighbours[mode]

    def _group_affinity(self, current, other):
        if current in self.group_paths and other in self.group_paths:
            left, right = self.group_paths[current], self.group_paths[other]
            return len(left & right) / len(left | right)
        if current in self.centroids and other in self.centroids:
            return _cosine(self.centroids[current], self.centroids[other])
        return 0.

    def _track_group_priority(self, current, other, nearby):
        affinity = self._group_affinity(current, other)
        return -self._now(other, affinity if nearby else -affinity) - .2 * self.group_allocation.get(other, 0.)

    def configure(self, values):
        self.tuning = radio_tuning.controls(values)

    def refresh_exclusions(self, now=None):
        """Use the live feedback clock even when prepared/current-layer data is older."""
        self.eligibility_at = max(self.eligibility_at, self.prepared_at, self.now_sampled_at,
                                  now if now is not None else self.eligibility_at)
        # Prepared planning holds a pinned view; otherwise acquire one reader
        # for both exclusions so feedback updates cannot split the two facts.
        if self.read_store is not self.store:
            self.blocked, self.muted = self.prepared.planning_fact('radio-exclusions',
                lambda: self.inputs.exclusions(self.read_store, self.eligibility_at))
        else:
            with self.inputs.read_view() as reader:
                self.blocked, self.muted = self.inputs.exclusions(reader, self.eligibility_at)

    def _now(self, mid, base=0.):
        return radio_tuning.now_bonus(base, self.now_posterior.get(str(mid), 0), self.now_gain)

    def _allowed(self, track):
        return allowed_seed(track, self.blocked, self.muted, self.maximum_length, self._genre_allowed)

    def _genre_allowed(self, label):
        if self._tuning_read and not self._has_genre_exclusions():
            return True
        if label and label not in self.axes and self._tuning_read:
            self._page_axes(self.read_store, ({'genre': label},))
        family, subgenre = self.axes.get(label, ('', None))
        return (self.genre_weights.get(f'genre:{family}', 1) != 0
                and self.genre_weights.get(f'genre:{subgenre}', 1) != 0)

    def _has_genre_exclusions(self):
        return self.prepared.planning_fact('radio-genre-exclusions',
            lambda: any(key.startswith('genre:') and weight == 0 for key, weight in self.genre_weights.items()))

    def _load_taxonomy(self):
        if self._scoped_axes and not self._tuning_read:
            self.axes.update(self.inputs.taxonomy())
            self._scoped_axes = False

    def _page_axes(self, reader, tracks):
        if self._scoped_axes:
            labels = {track.get('genre') for track in tracks} - self.axes.keys() - {None, ''}
            if labels:
                found = reader.genres.feature_axes(labels)
                self.axes.update({label: found.get(label, (None, None)) for label in labels})
                for family, key in found.values():
                    if key:
                        self.axes.setdefault(key, (family, key))

    def retain(self, tracks, *, rotation_loaded=False):
        """Retain session metadata when historical songs leave the prepared pool."""
        tracks = list(tracks)
        labels = {track.get('genre') for track in tracks} - self.axes.keys() - {None, ''}
        if labels:
            with self.inputs.read_view() as reader:
                if self._scoped_axes:
                    self._page_axes(reader, tracks)
                else:
                    self.axes.update(self.inputs.taxonomy_facts(reader, labels))
        if not rotation_loaded:
            self._rotation_facts(tracks)
        if self.prepared.generation is not None:
            score_tracks = ([track for track in tracks if '_radio_catalog_score' not in track]
                            if self._tuning_read else tracks)
            for start in range(0, len(score_tracks), CLUSTER_POOL_LIMIT):
                batch = score_tracks[start:start + CLUSTER_POOL_LIMIT]
                scores = self.prepared.track_facts('scores', [track['key'] for track in batch],
                    lambda keys: self.read_store.recommendations.ranks(self.prepared.surface('radio'), 'all', 'scores', keys,
                                                                      generation=self.prepared.generation))
                for track in batch:
                    score = scores.get(track['key'], {}).get('score')
                    if not track.get('_radio_fresh') or '_radio_catalog_score' not in track:
                        track['_radio_catalog_score'] = score or 0
        # Legacy saved sessions have no profile stamp. Hydrate those bounded
        # metadata records, while preserving profiles on already issued choices.
        self._energy([track for track in tracks if 'musical' not in track])
        for track in tracks:
            self.by_key.setdefault(track["key"], track)
            self.by_video.setdefault(track["video_id"], track)

    def _read_vectors(self, keys):
        keys = list(dict.fromkeys(k for k in keys if k not in self.vector_reads))
        if not keys:
            return
        self.vector_reads.update(keys)
        # Incremental placements retain these coordinates. Only a model refit changes them.
        store = self.read_store
        with store._lock:
            if store.rec_model.recording_encoder() != self.encoding_model:
                return
            for offset in range(0, len(keys), CLUSTER_POOL_LIMIT):
                batch = keys[offset:offset + CLUSTER_POOL_LIMIT]
                pools = store.rec_model.content_vector_pools(batch)
                for key, blob in {**pools['library'], **pools['discovered']}.items():
                    vector = np.frombuffer(blob, dtype=np.float32)
                    if not self.centroids or len(vector) == len(next(iter(self.centroids.values()))):
                        self.vectors[key] = _unit(vector)

    def _load(self, mid):
        if mid in self.loaded:
            return self.pools[mid]
        pool = self._initial_track_group_pool(mid)
        keys = [t["key"] for t in pool]
        store = self.read_store
        library = store.rec_query.tracks_by_keys(keys)
        videos = [t["video_id"] for t in pool]
        preferred = store.tracks.preferred_playback_uploads(videos)
        details = store.radio_catalog.details(videos)
        valid = []
        for old in pool:
            key = old["key"]
            if old["video_id"] not in details:
                continue
            track = {**old, **(preferred.get(old['video_id']) or details[old["video_id"]])}
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
        keys = [track['key'] for track in tracks]
        load = lambda keys: musical_character.load(self.read_store, keys)
        profiles = self.prepared.track_facts('profiles', keys, load)
        changed = False
        for track in tracks:
            profile = profiles.get(track['key'])
            changed |= track.get('musical') != profile
            # Overlapping mode pools share reads, while each choice keeps its
            # own mutable metadata just as separate hydration passes did.
            track['musical'] = deepcopy(profile)
        return changed

    def refresh_metadata(self, tracks):
        """Refresh only the visible menu's uploads after the shared worker enriches them."""
        videos = list(dict.fromkeys(t["video_id"] for t in tracks))[:32]
        if not videos:
            return False
        by_video = self.store.radio_catalog.details(videos)
        self.axes = self.inputs.taxonomy(track.get('genre') for track in by_video.values())
        changed = self._rotation_facts([track for track in tracks if track['video_id'] in videos])
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

    def _rotation_facts(self, tracks):
        keys = list(dict.fromkeys(track['key'] for track in tracks))
        if not keys:
            return False
        load = lambda keys: self.read_store.radio_catalog.rotation_facts(keys)
        facts = self.prepared.track_facts('rotation', keys, load)
        changed = False
        for track in tracks:
            fact = facts.get(track['key'])
            values = radio_rotation.metadata(fact)
            changed |= any(track.get(key) != value for key, value in values.items())
            track.update(values)
        return changed

    def fits(self, track, anchor=None):
        """Only known, clear mismatches invalidate a future song after enrichment."""
        if not self._allowed(track):
            return False
        if self.tuning["theme"] == 1 and not GenrePolicy(1, [], current=anchor, axes=self.axes).allows(track):
            return False
        return True

    def cluster_for(self, track):
        key = track["key"]
        if key in self.membership:
            return self.membership[key]
        if self.group_head and self.modes and not track.get('_radio_fresh'):
            version = self.read_store.taste_groups.leaf_members(self.group_head['generation'], keys=[key]).get(key)
            if version in self.pools:
                self.membership[key] = version
                return version
        if self.centroids:
            self._read_vectors([key])
        vector = self.vectors.get(key)
        if vector is not None and self.centroids:
            mid = max(self.centroids, key=lambda mid: _cosine(vector, self.centroids[mid]))
        else:
            family, genre = self.axes.get(track.get('genre'), (None, None))
            def affinity(mid):
                group = self.modes.get(mid, {})
                size = max(1, group.get('size', 0))
                return (sum(count for label, count in group.get('genres', ())
                            if genre and self.axes.get(label, (None, None))[1] == genre) / size,
                        sum(count for label, count in group.get('families', ()) if label == family) / size,
                        self.group_allocation.get(mid, 0.))
            mid = max(self.pools, key=affinity)
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
            axes = self.axes
            fa, fb = (axes.get(track.get('genre'), (None, None))[0] for track in (a, b))
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
        recent = history[-8:]
        return radio_ranking.candidate_score(track, prior=self.prior.get(mid, {}).get(track['key'],
            track.get('_radio_catalog_score', self.catalog_scores.get(track['key'], 0))),
            recent=[self.similarity(track, anchor) for anchor in reversed(recent)] if genres.breadth != 1 else [],
            target=self.similarity(track, target) if target and genres.breadth != 1 else None,
            feedback=[(strength, self.similarity(track, self.by_key[key]))
                      for key, strength in list((feedback or {}).items())[-24:] if key in self.by_key],
            tuning=self.tuning, breadth=genres.breadth, genre_bonus=genres.bonus(track),
            now_share=self.now_posterior.get(str(mid), 0), now_gain=self.now_gain,
            group_share=self.group_allocation.get(mid, 0.))

    def _choice_tier(self, eligible, history, target, genres, *, load_vectors=True, preference=None):
        eligible = list(eligible)
        allowed = [t for t in eligible if self._allowed(t) and genres.allows(t)]
        allowed = radio_intensity.narrow(allowed, preference)
        if genres.breadth == 1 and allowed:
            contrast = max(genres.contrast(t) for t in allowed)
            allowed = [t for t in allowed if genres.contrast(t) == contrast]
        if load_vectors:
            self._read_vectors([t['key'] for t in allowed])
        decisions = {t['key']: dict(allowed=True, contrast=genres.contrast(t),
            fits=self.fits(t, genres.current if genres.pinned else target), artist_gap=radio_rotation.artist_gap(t, history))
            for t in allowed}
        return radio_ranking.choice_tier(allowed, decisions, breadth=genres.breadth)

    def _choose(self, eligible, history, target, feedback, genres, *, other_anchors=(), sequence=False,
                prefetched=False, preference=None):
        eligible = self._choice_tier(eligible, history, target, genres, load_vectors=not prefetched,
                                     preference=preference)
        if not eligible:
            return None
        if not prefetched:
            self._rotation_facts([*eligible, *history[-radio_rotation.WINDOW:]])
        return radio_ranking.select_next(eligible, history, comfort=self.tuning['comfort'],
            now=max(self.prepared_at, self.now_sampled_at),
            score=lambda track: self._score(track, history, target, feedback, genres),
            similarity=self.similarity, other_anchors=other_anchors, rng=self.rng,
            journey=self.tuning.get('journey') if sequence else None, axes=self.axes)

    def _genre_modes(self, genres, excluded):
        """Inspect prepared metadata globally; hydrate/score at most four pools.

        Nearby groups can all share one genre. Endpoints must be able to reach
        a contrasting genre, or the pinned genre in another group, directly.
        """
        ranked = []
        for mid in self.pools:
            pool = self._initial_track_group_pool(mid)
            rows = [t for t in pool if t["video_id"] not in excluded
                    and self._allowed(t) and genres.allows(t)]
            if not rows:
                continue
            priority = max(genres.contrast(t) for t in rows) if not genres.pinned else (0., 0., 0.)
            ranked.append((mid, (priority, self._now(mid))))
        return [mid for mid, _ in sorted(ranked, key=lambda item: item[1], reverse=True)]

    def _genre_pick(self, history, target, feedback, genres, excluded, *, sequence=False):
        self.refresh_exclusions()
        modes = self._genre_modes(genres, excluded)[:GENRE_POOL_LIMIT]
        eligible = [t for mid in modes for t in self._load(mid) if t["video_id"] not in excluded]
        pick = self._choose(eligible, history, target, feedback, genres, sequence=sequence)
        if pick is not None or not genres.pinned:
            return pick
        return self._catalog_pick(history, target, feedback, genres, excluded, sequence=sequence)

    def _catalog_pick(self, history, target, feedback, genres, excluded, *, other_anchors=(), sequence=False,
                      preference=None):
        # Preserve the exact genre when prepared lookahead is exhausted.
        label = genres.key(genres.current)
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
            pool = radio_catalog.themed_pool(self.read_store, genres, excluded, used, self._allowed,
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
        return self._choose(eligible, history, target, feedback, genres, other_anchors=other_anchors,
                            sequence=sequence, preference=preference)

    def _group_pick(self, current, history, target, feedback, genres, excluded, *,
                    other_anchors=(), sequence=False):
        modes = self._ordered_track_groups(current)
        if self.group_head and genres.breadth == 1:
            self.refresh_exclusions()
            if not (self._tuning_read and self.prepared.has_intensity):
                modes = list(dict.fromkeys([*self._genre_modes(genres, excluded), current, *modes]))
            if self.prepared.has_intensity:
                with self.prepared.planning_read():
                    eligible = self._stored_intensity_candidates(history, excluded, genres, modes, None)
                return self._choose(eligible, history, target, feedback, genres,
                                    other_anchors=other_anchors, sequence=sequence)
            eligible = [track for mid in modes[:GENRE_POOL_LIMIT]
                        for track in self._cluster_pool(mid, history, excluded, genres=genres)]
            used = {track['key'] for track in [*history, *eligible]}
            used.update(self.by_video[video]['key'] for video in excluded if video in self.by_video)
            eligible = self._selection_tier(eligible, history, excluded, genres, used, modes,
                                            preference=None, reserved=())
            return self._choose(eligible, history, target, feedback, genres,
                                other_anchors=other_anchors, sequence=sequence)
        if genres.pinned:
            genre = genres.key(genres.current)
            modes[1:] = sorted(modes[1:], key=lambda mid: not any(
                self.axes.get(label, (None, None))[1] == genre
                for label, _ in self.modes.get(mid, {}).get('genres', ())))
        elif other_anchors:
            taken = {self.cluster_for(track) for track in other_anchors}
            modes.sort(key=lambda mid: mid in taken)
        self._read_vectors([track['key'] for track in other_anchors])
        wanted = radio_rotation.next_kind(history, self.tuning['comfort'])
        fallback = None
        for mid in _bounded_track_groups(modes, current):
            eligible = self._cluster_pool(mid, history, excluded, genres=genres)
            if not eligible:
                continue
            pick = self._choose(eligible, history, target, feedback, genres, other_anchors=other_anchors,
                                sequence=sequence)
            if pick and (pick['_radio_rotation'] == wanted or (genres.pinned and wanted != 'new')):
                return pick
            fallback = fallback or pick
        if self.group_head and wanted == 'new':
            used = {track['key'] for track in history}
            used.update(self.by_video[video]['key'] for video in excluded if video in self.by_video)
            fresh = self._fresh_reserve(excluded, used, genres)
            self.retain(fresh)
            pick = self._choose(fresh, history, target, feedback, genres, other_anchors=other_anchors,
                                sequence=sequence)
            if pick:
                return pick
        if fallback is None and genres.pinned:
            return self._catalog_pick(history, target, feedback, genres, excluded,
                                      other_anchors=other_anchors, sequence=sequence)
        return fallback

    def startup_tracks(self, history, count):
        """Fill the initial queue from one shortlist per visited prepared group."""
        if not self.lazy_modes or not self.cluster_members:
            return None
        selected = []
        seed_group = self.cluster_for(history[0])
        neighbours = self._neighbours(seed_group)
        with self.prepared.planning_read():
            self.refresh_exclusions()
            for group in (seed_group, *neighbours):
                if group not in self.cluster_members:
                    continue
                selected.extend(startup_shortlist(self.prepared, self.read_store, group, count-len(selected),
                    [*history, *selected], allowed=self._allowed, axes=self._page_axes,
                    sample=self.cluster_members[group] > 24))
                if len(selected) == count:
                    break
        return selected

    @property
    def prepared_streams(self):
        return (bool(self.lazy_modes and self.cluster_members)
                and self.tuning['theme'] == self.tuning['energy'] == .5)

    def continuation_groups(self, group):
        return (group, *self._neighbours(group))

    def stream_pool(self, group, history, excluded, *, limit=32):
        if group not in self.cluster_members:
            return []
        tracks = self.prepared.tracks('radio', group, 'membership', allowed=self._allowed,
            excluded=excluded, used={t['key'] for t in history}, limit=limit, include_display=False,
            sample_membership=self.cluster_members[group] > 32, prepare_page=self._page_axes)
        for track in tracks:
            track['_radio_mode'] = group
            self.membership.setdefault(track['key'], group)
        self.retain(tracks)
        self._read_vectors([t['key'] for t in [*tracks, *history[-8:]]])
        return tracks

    def tuned_heads(self, history, anchor, excluded, feedback, *, mix_position=None):
        """Share a shortlist across the four first picks on a prepared session."""
        if not self._tuning_read:
            return None
        self._page_axes(self.read_store, history[-8:])
        genres = GenrePolicy(self.tuning['theme'], history, axes=self.axes)
        preference = radio_intensity.preference(self.tuning['energy'], mix_position, history[-1] if history else None)
        context = self.cluster_for(anchor)
        order = self._ordered_track_groups(context)
        if preference is not None or genres.pinned or genres.breadth == 1:
            pool = self._tuned_group_shortlist(history, excluded, genres, order, preference)
            if pool is None:
                if not self.prepared.has_intensity:
                    return None
                pool = self._stored_intensity_candidates(history, excluded, genres, order, preference, heads=True)
        else:
            pool, groups = [], set()
            for group in dict.fromkeys([context, *order, *self.pools]):
                tracks = self.stream_pool(group, history, excluded, limit=16)
                if tracks:
                    groups.add(group)
                    pool.extend(tracks)
                if len(groups) == 4:
                    break
            wanted = radio_rotation.next_kind(history, self.tuning['comfort'])
            if wanted == 'new' and not any(track['_radio_rotation'] == wanted for track in pool):
                used = {track['key'] for track in [*history, *pool]}
                fresh = self._fresh_reserve(excluded, used, genres)
                self.retain(fresh)
                self._read_vectors([track['key'] for track in fresh])
                pool.extend(fresh)
        catalog = {track['key'] for track in pool if track.pop('_radio_catalog_only', False)}
        heads, groups = [], set()
        for _ in range(4):
            tier = radio_intensity.narrow(pool, preference)
            tier = [track for track in tier if track['key'] not in catalog] or tier
            distinct = [track for track in tier if self.cluster_for(track) not in groups]
            active = [track for track in distinct if self.cluster_for(track) == context] if not heads else []
            pick = self.next_in_pool(active or distinct or tier, history, anchor if not heads else None,
                                     feedback, excluded, other_anchors=heads)
            if pick is None:
                break
            heads.append(pick)
            groups.add(self.cluster_for(pick))
            excluded = excluded | {pick['video_id']}
            pool = [track for track in pool if track['key'] != pick['key']]
        return heads if len(heads) == 4 else None

    def _tuned_group_shortlist(self, history, excluded, genres, group_order, preference):
        pinned_genre = genres.key(genres.current) if genres.pinned and preference is not None else None
        rows = self.prepared.tuning_ranks('membership', energy=preference is not None,
                                          pinned_genre=pinned_genre,
                                          maximum_duration=self.maximum_length)
        if rows is None:
            return None
        used = {track['key'] for track in history}
        used.update(self.by_video[video]['key'] for video in excluded if video in self.by_video)
        order = {mid: index for index, mid in enumerate(dict.fromkeys([*group_order, *self.pools]))}
        by_group, catalog = defaultdict(list), defaultdict(list)
        eclectic = genres.breadth == 1 and genres.recent

        def priority(row):
            return radio_intensity.priority(row['intensity'], preference)

        def contrast(row):
            return tuple(-value for value in genres.contrast(row)) if eclectic else ()

        for row in self._eligible_tuning_ranks(rows, excluded, used, genres):
            if row['purpose'] == 'catalog':
                catalog[priority(row)].append(row)
            elif row['mode_key'] in self.cluster_members:
                by_group[row['mode_key']].append(row)
        groups = []
        for mid, members in by_group.items():
            members.sort(key=lambda row: (priority(row), contrast(row), row['rank']))
            witness = members[0]
            groups.append((priority(witness), contrast(witness), order[mid], mid))
        shortlist = [row for _, _, _, mid in sorted(groups)[:GENRE_POOL_LIMIT] for row in by_group[mid][:16]]
        # Keep enough distinct catalog alternatives for all four heads.
        published = {row['key'] for row in shortlist}
        alternatives = {}
        for tier in sorted(catalog):
            for row in catalog[tier]:
                if row['key'] not in published:
                    alternatives.setdefault(row['key'], row)
            if len(alternatives) >= 4:
                break
        shortlist.extend(list(alternatives.values())[:4])
        tracks = self.prepared.hydrate_tuning_ranks(shortlist)
        for row, track in zip(shortlist, tracks):
            if row['purpose'] == 'membership':
                track['_radio_mode'] = self.membership[track['key']] = row['mode_key']
            else:
                track['_radio_catalog_only'] = True
            track['_radio_catalog_score'] = row['score'] or 0
        self.retain(tracks)

        wanted = radio_rotation.next_kind(history, self.tuning['comfort'])
        if wanted == 'new' and not any(track['_radio_rotation'] == wanted for track in tracks):
            used.update(track['key'] for track in tracks)
            fresh = self.prepared.tuning_ranks('fresh', energy=preference is not None,
                                               maximum_duration=self.maximum_length)
            if fresh is None:
                reserve = self._fresh_reserve(excluded, used, genres)
            else:
                eligible = sorted(self._eligible_tuning_ranks(fresh, excluded, used, genres), key=priority)
                reserve = self.prepared.hydrate_tuning_ranks(eligible[:CLUSTER_POOL_LIMIT])
                for index, track in enumerate(reserve):
                    track['_radio_catalog_score'] = 1 - index / CLUSTER_POOL_LIMIT
            self.retain(reserve)
            tracks.extend(track for track in reserve if track['_radio_rotation'] == 'new')
        self._read_vectors([track['key'] for track in tracks])
        return tracks

    def _eligible_tuning_ranks(self, rows, excluded, used, genres):
        self._page_axes(self.read_store, rows)
        for row in rows:
            if row['key'] not in used and row['video_id'] not in excluded and self._allowed(row) and genres.allows(row):
                yield row

    def tuned_fill_picker(self, history, *, energy, current=None):
        """Share current candidate evidence across positions in one planning pass."""
        current_genre = GenrePolicy(self.tuning['theme'], history, current=current, axes=self.axes)
        pinned_genre = current_genre.key(current_genre.current) if current_genre.pinned else None
        return self.prepared.planning_fact(('radio-fill-picker', energy, pinned_genre),
            lambda: self._ranked_fill_picker(history, energy, pinned_genre))

    def _ranked_fill_picker(self, history, energy, pinned_genre):
        rows = self.prepared.tuning_ranks('membership', energy=energy or bool(self.tuning.get('journey')),
                                          include_fresh=True, pinned_genre=pinned_genre,
                                          maximum_duration=self.maximum_length)
        if rows is None:
            return None
        self._page_axes(self.read_store, rows)
        video_keys = {row['video_id']: row['key'] for row in rows}
        bands, catalog = defaultdict(list), defaultdict(list)
        for row in rows:
            if row['purpose'] == 'membership' and row['mode_key'] not in self.pools:
                continue
            if not self._allowed(row):
                continue
            row.update(radio_rotation.metadata(dict(heard=row['heard'], owned=not row['_radio_fresh'],
                                                     last_started=row['last_started'], last_offered=row['last_offered'])))
            lane = catalog if row['purpose'] == 'catalog' else bands
            lane[radio_intensity.band(row['intensity'])].append(row)
        hydrated = {}
        self._rotation_facts(history[-radio_rotation.WINDOW:])

        def pick(sequence, target, feedback, excluded, current, position, reserved, *, other_anchors=()):
            check_cancellable_work()
            genre_history = [*sequence, *other_anchors] if other_anchors else sequence
            genres = GenrePolicy(self.tuning['theme'], genre_history, current=current, axes=self.axes)
            wanted = radio_rotation.next_kind(sequence, self.tuning['comfort'])
            preference = radio_intensity.preference(self.tuning['energy'], position, sequence[-1] if sequence else None)
            used = {track['key'] for track in sequence}
            used.update(self.by_video[video]['key'] for video in excluded if video in self.by_video)
            used.update(video_keys[video] for video in excluded if video in video_keys)
            eligible = self._fill_tier(bands, catalog, preference, wanted, used, excluded, genres)
            if not eligible:
                return None
            if reserved:
                eligible = [row for row in eligible if row['video_id'] not in reserved] or eligible
            context = self.cluster_for(target or current) if target or current else self._context(sequence)
            order = list(dict.fromkeys([*self._ordered_track_groups(context), *self.pools]))
            if other_anchors and not genres.pinned:
                taken = {self.cluster_for(track) for track in other_anchors}
                order.sort(key=lambda group: group in taken)
            rows = self._fill_shortlist(eligible, sequence, genres, order, wanted)
            missing = [row for row in rows if (row['pool_id'], row['rank']) not in hydrated]
            new = self.prepared.hydrate_tuning_ranks(missing)
            for row, track in zip(missing, new, strict=True):
                if row['purpose'] == 'membership':
                    track['_radio_mode'] = self.membership[track['key']] = row['mode_key']
                    track['_radio_catalog_score'] = row['score'] or 0
                elif row['purpose'] == 'fresh':
                    track['_radio_catalog_score'] = 1 / (1 + row['rank'])
                else:
                    track['_radio_catalog_score'] = 0.
                for field in ('_radio_rotation', '_radio_fresh', '_radio_last_started', '_radio_last_offered'):
                    track[field] = row[field]
                track['_radio_intensity'] = row['intensity_evidence']
                hydrated[row['pool_id'], row['rank']] = track
            self._read_vectors([track['key'] for track in new])
            tracks = [hydrated[row['pool_id'], row['rank']] for row in rows]
            if genres.breadth != 1 and rows[0]['purpose'] != 'catalog':
                tracks = self._rotation_group_tracks(tracks, sequence, order, prefetched=True)
            return self._choose(tracks, sequence, target, feedback, genres, sequence=not other_anchors,
                                prefetched=True, other_anchors=other_anchors)

        return pick

    def finish_tuned_fill(self, tracks):
        picks = [track for track in tracks if '_radio_intensity' in track]
        for track in picks:
            track.pop('_radio_intensity')
        self.retain(picks, rotation_loaded=True)

    @staticmethod
    def _fill_tier(bands, catalog, preference, wanted, used, excluded, genres):
        """Try the best energy tier first, admitting Fresh when rotation calls for it."""
        def eligible(rows):
            matches = {}
            for row in rows:
                if row['key'] in used or row['video_id'] in excluded:
                    continue
                label = row['genre']
                if label not in matches:
                    matches[label] = genres.allows(row)
                if matches[label]:
                    yield row

        if preference is None:
            tiers = (([row for rows in bands.values() for row in rows],
                      [row for rows in catalog.values() for row in rows]),)
        else:
            levels = sorted(bands.keys() | catalog.keys(), key=lambda band: radio_intensity.band_priority(band, preference))
            tiers = ((bands.get(band, ()), catalog.get(band, ())) for band in levels)
        fresh_fallback = []
        for tier, exact_genre in tiers:
            members, fresh = {}, {}
            for row in eligible(tier):
                dest = fresh if row['purpose'] == 'fresh' else members
                dest.setdefault(row['key'], row)
            if wanted == 'new' and (members or fresh):
                return list({**fresh, **members}.values())
            if members:
                return list(members.values())
            if fresh and not fresh_fallback:
                fresh_fallback = list(fresh.values())
            if preference is None and fresh_fallback:
                return fresh_fallback
            matches = list(eligible(exact_genre))
            if matches:
                return matches
        return fresh_fallback

    def _live_energy_pick(self, history, target, feedback, excluded, current, position, reserved, *,
                          other_anchors=()):
        with self.prepared.planning_read():
            evidence = [*history[-8:], *other_anchors]
            if current is not None:
                evidence.append(current)
            self.tuning_inputs(evidence, self.eligibility_at)
            picker = self.tuned_fill_picker(history, energy=True, current=current)
            if picker is None:
                return None
            pick = picker(history, target, feedback, excluded, current, position, reserved,
                          other_anchors=other_anchors)
            if pick is not None:
                self.finish_tuned_fill([pick])
            return pick

    @staticmethod
    def _fill_shortlist(rows, history, genres, order, wanted):
        if genres.breadth == 1:
            contrast = {label: genres.contrast({'genre': label}) for label in {row['genre'] for row in rows}}
            best = max(contrast.values())
            rows = [row for row in rows if contrast[row['genre']] == best]
        groups = defaultdict(list)
        for row in rows:
            groups[row['mode_key'] if row['purpose'] == 'membership' else None].append(row)
        group_rank = {group: index for index, group in enumerate(order)}
        members = sorted((group for group in groups if group is not None),
                         key=lambda group: (not any(row['_radio_rotation'] == wanted for row in groups[group]),
                                            group_rank[group]))
        # Only the shortlist needs vectors. The energy and genre tiers above
        # consider every eligible published recording.
        result = []
        for group in [*members[:GENRE_POOL_LIMIT if genres.breadth == 1 else 1], None]:
            ranked = sorted(groups[group], key=lambda row: (-radio_rotation.artist_gap(row, history), row['rank']))
            result.extend(radio_rotation.reserve(ranked, 16, .5) if wanted is not None else ranked[:16])
        return result

    def alternative_pools(self, history, active, retained, count, excluded, depth):
        """Choose the groups for the menu before scoring any of its anchors."""
        context = self.cluster_for(history[0]) if len(history) == 1 else self._context(history)
        active_group = self.cluster_for(active)
        taken = [self.cluster_for(track) for track in retained]
        groups = [mid for mid in self.pools if mid in self.cluster_members and mid not in [active_group, *taken]]
        result, short = [], []
        while groups and len(result) < count:
            check_cancellable_work()

            def priority(group):
                overlap = max((self._group_affinity(group, old) for old in taken), default=0.)
                return self._track_group_priority(context, group, True) + .65 * overlap

            group = min(groups, key=priority)
            groups.remove(group)
            pool = self.stream_pool(group, [*history, active, *retained], excluded)
            if len(pool) >= depth:
                result.append(pool)
                taken.append(group)
            elif pool:
                short.append(pool)
        return (result + short)[:count]

    def next_in_pool(self, pool, history, target, feedback, excluded, *, other_anchors=(), include_fresh=False,
                     mix_position=None):
        keys = {t['key'] for t in history}
        keys.update(self.by_video[video]['key'] for video in excluded if video in self.by_video)
        eligible = [track for track in pool if track['video_id'] not in excluded and track['key'] not in keys]
        genres = GenrePolicy(self.tuning['theme'], history, axes=self.axes)
        if include_fresh and self.group_head and pool and radio_rotation.next_kind(history, self.tuning['comfort']) == 'new':
            # Admit discoveries in the stream's track group or sharing its exact
            # genre, then use the normal choice tier.
            reference = pool[0]
            group, genre_key = self.cluster_for(reference), genres.key(reference)
            fresh = self._fresh_reserve(excluded, keys | {track['key'] for track in eligible}, genres)
            self.retain(fresh)
            self._read_vectors([track['key'] for track in fresh])
            eligible.extend(track for track in fresh
                            if self.cluster_for(track) == group or (genre_key and genres.key(track) == genre_key))
        preference = radio_intensity.preference(self.tuning['energy'], mix_position, history[-1] if history else None)
        return self._choose(eligible, history, target, feedback, genres, other_anchors=other_anchors,
                            sequence=target is not None, prefetched=True, preference=preference)

    def seed_offer(self, n=16):
        """Spread the art grid across prepared regions with bounded sparse-pool hydration."""
        with self.prepared.planning_read():
            return self._seed_offer(n)

    def _seed_offer(self, n):
        self._load_taxonomy()
        self.refresh_exclusions()
        modes = list(self.pools)
        self.rng.shuffle(modes)
        if self.group_allocation:
            modes.sort(key=lambda mid: self._seed_priority(mid))
        if self.cluster_members:
            for mid in modes[:min(n, GENRE_POOL_LIMIT)]:
                self._cluster_pool(mid, [], set())
        selected, used = [], set()
        while len(selected) < n:
            added = False
            for mid in modes:
                eligible = [t for t in self._initial_track_group_pool(mid)
                            if t["video_id"] not in used and self._allowed(t)]
                if not eligible:
                    continue
                # The seed establishes the genre; an empty history cannot pin it.
                genres = GenrePolicy(.5, [], axes=self.axes)
                track = self._choose(eligible, selected, None, None, genres)
                if track is None:
                    continue
                selected.append(track)
                used.add(track["video_id"])
                added = True
                if len(selected) == n:
                    break
            if not added:
                break
        return selected

    def _seed_priority(self, mid):
        # Keep every group in the rotation, but spend the earliest slots in
        # proportion to adaptive allocation when groups outnumber the offer.
        weight = .1 / len(self.pools) + .9 * self.group_allocation.get(mid, 0.)
        return -math.log(max(self.rng.random(), 1e-12)) / weight

    def next_track(self, history, *, excluded=(), target=None, feedback=None, current=None,
                   destination=(), recipe=None, policy='upload', mix_position=None, reserved=()):
        self._load_taxonomy()
        # Callers supply the persisted destination facts on fresh and resumed
        # sessions. Keep today's exact-upload exclusion contract explicit.
        if policy != 'upload':
            raise ValueError('unsupported Radio membership policy')
        excluded = set(excluded) | {video for _, video in destination if video}
        preference = radio_intensity.preference(self.tuning['energy'], mix_position, history[-1] if history else None)
        if preference is not None:
            excluded = set(excluded) | {t['video_id'] for t in history}
            if (mix_position is not None and self.lazy_modes and self.cluster_members
                    and self.prepared.generation is not None):
                return self._live_energy_pick(history, target, feedback, excluded, current, mix_position, reserved)
            context = self.cluster_for(target or current or history[-1]) if (target or current or history) else next(iter(self.pools))
            genres = GenrePolicy(self.tuning['theme'], history, current=current, axes=self.axes)
            groups = self._intensity_group_order(context, genres, excluded)
            eligible = self._intensity_candidates(history, excluded, genres, groups, preference, reserved=reserved)
            pick = self._choose(eligible, history, target, feedback, genres, sequence=True)
            if pick is None and genres.pinned:
                return self._catalog_pick(history, target, feedback, genres, excluded, sequence=True,
                                          preference=preference)
            return pick
        if self.cluster_members:
            excluded = set(excluded) | {t['video_id'] for t in history}
            observed = current or (history[-1] if history else None)
            destination = observed if self.tuning['theme'] == 1 else target or observed
            context = self.cluster_for(destination) if destination else next(iter(self.pools))
            genres = GenrePolicy(self.tuning['theme'], history, current=current, axes=self.axes)
            return self._group_pick(context, history, target, feedback, genres, excluded, sequence=True)
        genres = GenrePolicy(self.tuning["theme"], history, current=current, axes=self.axes)
        excluded = set(excluded) | {t["video_id"] for t in history}
        if genres.pinned or genres.breadth == 1:
            return self._genre_pick(history, target, feedback, genres, excluded, sequence=True)
        context = self._context(history)
        destination = self.cluster_for(target) if target else context
        # The middle follows nearby track groups. The eclectic side adds genre contrast.
        modes = [destination] if self.tuning["theme"] > .8 else list(dict.fromkeys((context, destination)))
        if genres.breadth:
            modes += [mid for mid in self._genre_modes(genres, excluded) if mid not in modes][:2]
        modes = list(dict.fromkeys(modes))
        order = modes + [mid for mid in self._neighbours(context) if mid not in modes]
        eligible, local = [], []
        wanted = radio_rotation.next_kind(history, self.tuning['comfort'])
        for index, mid in enumerate(order[:GENRE_POOL_LIMIT], 1):
            eligible.extend(t for t in self._load(mid) if t['video_id'] not in excluded)
            if index < len(modes):
                continue
            tier = self._choice_tier(eligible, history, target, genres)
            if not any(radio_rotation.artist_gap(t, history) > radio_rotation.ARTIST_GAP
                       and self.fits(t, target) for t in tier):
                continue
            local = local or list(eligible)
            self._rotation_facts(tier)
            if any(t['_radio_rotation'] == wanted for t in tier):
                return self._choose(eligible, history, target, feedback, genres, sequence=True)
        # An unavailable category can borrow from the local track group. Stay in
        # that group when a wider search could not supply the category.
        return self._choose(local or eligible, history, target, feedback, genres, sequence=True)

    def direction_anchor(self, history, other_anchors, *, excluded=(), feedback=None, mix_position=None):
        self._load_taxonomy()
        preference = radio_intensity.preference(self.tuning['energy'], mix_position, history[-1] if history else None)
        if preference is not None:
            excluded = set(excluded) | {t['video_id'] for t in [*history, *other_anchors]}
            if (mix_position is not None and self.lazy_modes and self.cluster_members
                    and self.prepared.generation is not None):
                return self._live_energy_pick(history, None, feedback, excluded, history[-1] if history else None,
                                              mix_position, (), other_anchors=other_anchors)
            current = self._context(history)
            genres = GenrePolicy(self.tuning['theme'], history + other_anchors,
                                 current=history[-1] if history else None, axes=self.axes)
            groups = self._intensity_group_order(current, genres, excluded)
            taken = {self.cluster_for(track) for track in other_anchors}
            if self.tuning['theme'] < 1:
                groups.sort(key=lambda mid: mid in taken)
            eligible = self._intensity_candidates(history, excluded, genres, groups, preference)
            pick = self._choose(eligible, history, None, feedback, genres)
            if pick is None and genres.pinned:
                return self._catalog_pick(history, None, feedback, genres, excluded, other_anchors=other_anchors,
                                          preference=preference)
            return pick
        if self.cluster_members:
            excluded = set(excluded) | {t['video_id'] for t in [*history, *other_anchors]}
            current = (self.cluster_for(history[-1]) if self.tuning['theme'] == 1 and history
                       else self._context(history))
            genres = GenrePolicy(self.tuning['theme'], history, axes=self.axes)
            return self._group_pick(current, history, None, feedback, genres, excluded, other_anchors=other_anchors)
        genres = GenrePolicy(self.tuning["theme"], history + other_anchors,
                             current=history[-1] if history else None, axes=self.axes)
        excluded = set(excluded) | {t["video_id"] for t in [*history, *other_anchors]}
        if genres.pinned or genres.breadth == 1:
            return self._genre_pick(history, None, feedback, genres, excluded)
        current = self._context(history)
        taken = {self.cluster_for(t) for t in other_anchors}
        # Apply the categorical preference before shortlisting nearby track groups.
        neighbours = sorted(self._neighbours(current), key=lambda mid: self._now(mid), reverse=True)
        # Alternatives take distinct local edges, without a farthest-song search.
        destinations = [mid for mid in neighbours if mid not in taken]
        if not destinations:
            destinations = [mid for mid in [current, *neighbours] if mid not in taken]
        if not destinations:
            destinations = [current, *neighbours]
        # Prefer distinct track groups while avoiding a repeat of an artist that just played.
        destinations += [mid for mid in [current, *neighbours] if mid not in destinations]
        eligible = []
        target = history[-1] if history and self.tuning["theme"] > .8 else None
        for mid in _bounded_track_groups(destinations, current):
            eligible += [t for t in self._load(mid) if t["video_id"] not in excluded]
            if any(radio_rotation.artist_gap(t, history) > radio_rotation.ARTIST_GAP
                   and self.fits(t, target) for t in eligible):
                break
        return self._choose(eligible, history, target, feedback, genres)
