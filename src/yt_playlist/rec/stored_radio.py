"""A live Radio reader over one retained, immutable recommendation publication."""
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import copy, deepcopy
from itertools import zip_longest
import hashlib
import weakref

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import group_roots, musical_character, radio_intensity, radio_rotation, vibe_intensity
from yt_playlist.rec.vibe_score_policy import score_patterns

PAGE_SIZE = 128
LOOKAHEAD = 96
HOME_LANES = ('wheelhouse', 'explore', 'comfort', 'temporal', 'fresh')
PUBLICATION_SURFACES = ('radio', 'home', 'fresh')
_planning_read = ContextVar('radio_planning_read', default=None)


def _cached(cache, key, load, limit):
    if key not in cache:
        cache[key] = load()
        if len(cache) > limit:
            cache.popitem(last=False)
    cache.move_to_end(key)
    return cache[key]


def initial_pool(bucket, mid, blocked, muted, comfort):
    """Interleave prepared lanes into the bounded live discovery reserve."""
    names = HOME_LANES
    pool, seen = [], set()
    for group in zip_longest(*(bucket.get(name, []) for name in names)):
        for name, item in zip(names, group):
            if not item or not item.get('video_id') or item['key'] in seen or item['key'] in blocked:
                continue
            if (item.get('artist') or '').casefold() in muted:
                continue
            pool.append({**item, '_radio_fresh': name == 'fresh',
                         '_radio_comfort': name == 'comfort', '_radio_mode': mid})
            seen.add(item['key'])
    fresh = [track for track in pool if track.get('_radio_fresh')]
    familiar = [track for track in pool if not track.get('_radio_fresh')]
    quota = round(LOOKAHEAD * .85 * (1 - comfort))
    reserve = fresh[:quota] + familiar[:LOOKAHEAD - min(quota, len(fresh))]
    used = {track['key'] for track in reserve}
    return (reserve + [track for track in pool if track['key'] not in used])[:LOOKAHEAD]


class PreparedRadio:
    def __init__(self, store, at, *, generation=None, surface_prefix=''):
        self.store = store
        self.surface_prefix = surface_prefix
        self.rotation_seed = str(at)
        self.comfort = .5
        self.rotation_cursors = {}
        self._intensity_headings = None
        surfaces = tuple(self.surface(name) for name in PUBLICATION_SURFACES)
        if not getattr(store, '_read_only', False) and store.activity_paused:
            # Select the published generation during cleanup. The live session
            # saves its lease when library writes resume.
            with store.read_view() as reader:
                selected, _ = group_roots.pending_head(reader, surfaces, at)
            self.generation, close = generation if generation is not None else selected, None
        elif generation is None:
            self.generation, close = group_roots.pending_head(store, surfaces, at)
        else:
            self.generation = generation
            close = None if getattr(store, '_read_only', False) else group_roots.pending(
                store, {'recommendation_generations': [generation]}, at)
        self._lifetime = weakref.finalize(self, close) if close is not None else None
        self.has_intensity = False

    def surface(self, name):
        return self.surface_prefix + name

    def close(self):
        owner = getattr(self, '_lease_owner', self)
        if owner._lifetime is not None:
            owner._lifetime()

    def for_tuning(self, comfort):
        reader = copy(self)
        reader.comfort = comfort
        reader.rotation_cursors = dict(self.rotation_cursors)
        # Keep the original retain marker alive for all plans on this publication.
        reader._lease_owner = getattr(self, '_lease_owner', self)
        return reader

    def retain(self, at):
        """Retain the selected generation once library writes resume."""
        owner = getattr(self, '_lease_owner', self)
        if (owner._lifetime is None and self.generation is not None
                and not getattr(self.store, '_read_only', False) and not self.store.activity_paused):
            close = group_roots.pending(self.store, {'recommendation_generations': [self.generation]}, at)
            if close is not None:
                owner._lifetime = weakref.finalize(owner, close)

    def _reader(self):
        if self.current_reader is not None:
            return nullcontext(self.current_reader)
        return nullcontext(self.store) if getattr(self.store, '_read_only', False) else self.store.read_view()

    @property
    def current_reader(self):
        scope = _planning_read.get()
        return scope['reader'] if scope is not None and scope['owner'] is self else None

    @contextmanager
    def planning_read(self):
        """Reuse bounded pages only within one committed-data planning pass.

        Every subsequent pass rehydrates current catalog/genre meanings. The
        context stays local to its thread/task; concurrent candidate readers
        cannot borrow its connection or retain its cache after it closes.
        """
        if self.current_reader is not None:
            yield
            return
        with (nullcontext(self.store) if getattr(self.store, '_read_only', False)
              else self.store.read_view(seconds=None)) as reader:
            with reader.recommendations.planning_read():
                token = _planning_read.set(dict(owner=self, reader=reader, pages=OrderedDict(),
                                               headings=OrderedDict(), axes=OrderedDict(), facts={}))
                try:
                    yield
                finally:
                    _planning_read.reset(token)

    def _cached_read(self, name, key, load, limit):
        scope = _planning_read.get()
        if scope is None or scope['owner'] is not self:
            return load()
        return _cached(scope[name], key, load, limit)

    def planning_fact(self, key, load):
        """Share snapshot facts, never history-dependent rankings, across streams."""
        scope = _planning_read.get()
        if scope is None or scope['owner'] is not self:
            return load()
        if key not in scope['facts']:
            scope['facts'][key] = load()
        return scope['facts'][key]

    def track_facts(self, name, keys, load):
        """Batch point reads once per recording in this planning snapshot."""
        if self.current_reader is None:
            return load(keys)
        cache = self.planning_fact(name, OrderedDict)
        missing = list(dict.fromkeys(key for key in keys if key not in cache))
        loaded = load(missing) if missing else {}
        for key in missing:
            cache[key] = loaded.get(key)
        result = {key: cache[key] for key in keys if cache[key] is not None}
        while len(cache) > 8192:
            cache.popitem(last=False)
        return result

    def genre_axes(self, label):
        with self._reader() as reader:
            return self._cached_read('axes', label,
                lambda: reader.genres.feature_axes([label]).get(label, (None, None)), 512)

    def tuning_ranks(self, purpose, *, energy, include_fresh=False, pinned_genre=None, maximum_duration=None):
        if self.generation is None:
            return None
        surface = self.fresh_surface if purpose == 'fresh' else 'radio'
        with self._reader() as reader:
            intensity = None
            if energy:
                measurement, joint, _estimate = score_patterns()
                intensity = (musical_character.VERSION, measurement, joint, vibe_intensity.SOURCES)
            fresh_surface = self.surface(self.fresh_surface) if include_fresh else None
            return self.planning_fact(('tuning-ranks', purpose, energy, include_fresh, pinned_genre, maximum_duration),
                lambda: reader.radio_catalog.tuning_ranks(
                    self.generation, self.surface(surface), purpose, intensity=intensity,
                    fresh_surface=fresh_surface, pinned_genre=pinned_genre, maximum_duration=maximum_duration))

    def hydrate_tuning_ranks(self, rows):
        with self._reader() as reader:
            return reader.recommendations.hydrate_radio_ranks(rows, self.generation)

    def tracks(self, surface, mode, purpose, *, allowed, excluded=(), used=(), limit=LOOKAHEAD,
               band=None, genres=None, reader=None, include_display=True, unheard_discoveries=False,
               sample_membership=True, prepare_page=None):
        """Advance past unavailable and rejected rows without a global cutoff."""
        if type(limit) is not int or not 1 <= limit <= LOOKAHEAD:
            raise ValueError('Invalid Radio lookahead limit')
        if self.generation is None:
            return []
        rotate = sample_membership and surface == 'radio' and purpose == 'membership' and limit > 1
        # Inspect at most 384 eligible rows for rotation, then adopt only the
        # requested lookahead. Quotas never trigger a full-membership search.
        budget = 3 * PAGE_SIZE if rotate else limit
        result, after, start, wrapped, seen = [], -1, -1, False, set(used)
        with (nullcontext(reader) if reader is not None else self._reader()) as current:
            def finish():
                if not rotate:
                    return result
                keys = [track['key'] for track in result]
                facts = self.track_facts('rotation', keys, current.radio_catalog.rotation_facts)
                for track in result:
                    track.update(radio_rotation.metadata(facts.get(track['key'])))
                return radio_rotation.reserve(result, limit, self.comfort)

            if sample_membership and surface == 'radio' and purpose == 'membership':
                pool = self._cached_read('headings', ('rotation', mode), lambda: current.recommendations.pool(
                    self.surface(surface), mode, purpose, generation=self.generation), 128)
                count = pool['eligible_count'] if pool else 0
                if count > LOOKAHEAD:
                    token = repr((self.rotation_seed, mode, band, genres)).encode()
                    start = after = int.from_bytes(hashlib.sha256(token).digest()[:8]) % count - 1
                    if rotate:
                        start = after = self.rotation_cursors.get((mode, band, tuple(genres or ())), start)
            inspected = 0
            while len(result) < budget:
                check_cancellable_work()
                key = (surface, mode, purpose, after, band, tuple(genres) if genres is not None else None,
                       include_display)
                page = self._cached_read('pages', key, lambda: current.recommendations.candidate_page(
                    self.surface(surface), mode, purpose, generation=self.generation, after=after,
                    limit=PAGE_SIZE, band=band, genres=genres, include_display=include_display), 256)
                after = page['after']
                if rotate:
                    self.rotation_cursors[mode, band, tuple(genres or ())] = -1 if page['exhausted'] else after
                inspected += len(page['tracks'])
                rows = page['tracks']
                if prepare_page is not None:
                    prepare_page(current, rows)
                stop = wrapped and any(track['pool_rank'] > start for track in rows)
                if unheard_discoveries:
                    facts = self.track_facts('rotation', [track['key'] for track in rows],
                                             current.radio_catalog.rotation_facts)
                    rows = [track for track in rows if radio_rotation.classify(facts.get(track['key'])) == 'new']
                eligible = (track for track in rows if (not wrapped or track['pool_rank'] <= start)
                            and track['key'] not in seen and track['video_id'] not in excluded and allowed(track))
                for track in eligible:
                    if track['key'] in seen:
                        continue
                    # Issued choices gain mode/profile metadata. Never let that
                    # mutate a cached catalog page used by another branch.
                    track = deepcopy(track)
                    if surface == 'radio' and purpose == 'membership':
                        track['_radio_catalog_score'] = track.get('score') or 0
                    result.append(track)
                    seen.add(track['key'])
                    if len(result) == budget:
                        break
                if stop:
                    break
                if rotate and inspected >= 3 * PAGE_SIZE and len(result) >= limit:
                    break
                if page['exhausted']:
                    if wrapped or start < 0:
                        break
                    after, wrapped = -1, True
            return finish()

    def capture_inputs(self, reader, modes, space, model_generation, blocked, muted, comfort, *, adaptive=False):
        """Capture the live lookahead and file owners from one pinned SQL view."""
        self.comfort = comfort
        if self.generation is None:
            return {}, [], []
        repo = reader.recommendations
        framing = repo.framing(self.surface('home'), generation=self.generation) or repo.framing(self.surface('radio'), generation=self.generation) or {}
        same_space = framing.get('radio_space') == space
        compatible_legacy = same_space and (self.surface_prefix or framing.get('radio_generation') == model_generation)
        # Adaptive membership is the published group partition. A newer encoder
        # does not change the recordings identified by an immutable group key.
        members = {}
        if same_space or adaptive:
            members = {key: row['eligible_count'] for row in
                       repo.selections(self.surface('radio'), 'membership', generation=self.generation)
                       if (key := row['mode_key']) in modes
                       and (compatible_legacy or modes[key].get('version') == key)}
        self.has_intensity = bool(members and framing.get('intensity_version') == radio_intensity.VERSION
                                  and framing.get('radio_profile_version') == musical_character.VERSION)
        result = {'_meta': {'musical_generation': framing.get('musical_generation')},
                  '_radio': {'generation': framing.get('radio_generation'), 'space': framing.get('radio_space'),
                             'members': members}}
        allowed = lambda track: track['key'] not in blocked and (track.get('artist') or '').casefold() not in muted
        self.fresh_surface = ('fresh' if repo.pool(self.surface('fresh'), 'all', 'fresh',
                                                  generation=self.generation) else 'home')
        if adaptive:
            # Home pools have their own group versions. A stale Radio catalog
            # does not force us to open their ranked pages before the first pick.
            home_modes = {row['mode_key'] for lane in HOME_LANES for row in
                          repo.selections(self.surface('home'), lane, generation=self.generation)
                          if row['eligible_count'] and row['mode_key'] in modes}
            if home_modes or any(members.values()):
                result['_lazy_modes'] = home_modes | {key for key, count in members.items() if count}
                return result, [], []
            modes = ()

        displays, pools = [], []

        # Lanes share many recordings. Hydrate their bounded pages together,
        # rather than repeating current catalog/genre reads for every lane.
        selected = self._home_lanes(reader, modes, allowed)

        def tracks(surface, mode, lane):
            rows = (selected[mode, lane] if surface == 'home' and (mode, lane) in selected else
                    self.tracks(surface, mode, lane, reader=reader, allowed=allowed, include_display=False))
            ranks = [track['pool_rank'] for track in rows]
            chunks = repo.display_references(self.surface(surface), mode, lane, ranks, generation=self.generation)
            displays.append((self.surface(surface), rows, ranks, chunks))
            return rows

        for mode in modes:
            buckets = {lane: tracks('home', mode, lane) for lane in HOME_LANES}
            result[mode] = {'_radio_pool': initial_pool(buckets, mode, blocked, muted, comfort)}
            pools.append((mode, buckets, blocked, muted, comfort))
        has_groups = any(result[mode]['_radio_pool'] for mode in modes) or any(members.values())
        result['all'] = {'fresh': tracks(self.fresh_surface, 'all', 'fresh')}
        if not has_groups:
            result['all']['wheelhouse'] = tracks('home', 'all', 'wheelhouse')
        return result, displays, pools

    def _home_lanes(self, reader, modes, allowed):
        selected = {(mode, lane): [] for mode in modes for lane in HOME_LANES}
        pending = [(mode, lane, -1) for mode, lane in selected]
        while pending:
            remaining = []
            for offset in range(0, len(pending), 16):
                check_cancellable_work()
                batch = pending[offset:offset + 16]
                pages = reader.recommendations.candidate_pages(self.surface('home'), batch,
                    generation=self.generation, limit=PAGE_SIZE, include_display=False)
                for (mode, lane, _), page in zip(batch, pages):
                    tracks = selected[mode, lane]
                    for track in page['tracks']:
                        if allowed(track):
                            tracks.append(deepcopy(track))
                            if len(tracks) == LOOKAHEAD:
                                break
                    if len(tracks) < LOOKAHEAD and not page['exhausted']:
                        remaining.append((mode, lane, page['after']))
            pending = remaining
        return selected

    def initial_track_group_pool(self, mode, blocked, muted, comfort):
        allowed = lambda track: track['key'] not in blocked and (track.get('artist') or '').casefold() not in muted
        with self.planning_read():
            selected = self._home_lanes(self.current_reader, (mode,), allowed)
            buckets = {lane: selected[mode, lane] for lane in HOME_LANES}
            pool = initial_pool(buckets, mode, blocked, muted, comfort)
            # Only the tracks selected for the combined reserve need display
            # data. Lane flags are determined by the original ranked pools.
            for lane in HOME_LANES:
                tracks = [track for track in pool if track['lane'] == lane]
                if not tracks:
                    continue
                display = self.current_reader.recommendations.display(self.surface('home'), mode, lane,
                    [track['pool_rank'] for track in tracks], generation=self.generation)
                for track in tracks:
                    explanation = display.get(track['pool_rank'], {})
                    track['reason'] = explanation.get('reason', '')
                    track['lane'] = explanation.get('lane', lane)
            return pool

    @staticmethod
    def decode_inputs(captured):
        from yt_playlist.core.recommendation_files import decode_display
        result, displays, pools = captured
        for surface, tracks, ranks, chunks in displays:
            explanations = decode_display(chunks, ranks, surface)
            for track in tracks:
                explanation = explanations.get(track['pool_rank'], {})
                track['reason'] = explanation.get('reason', '')
                track['lane'] = explanation.get('lane', track['lane'])
        for mode, buckets, blocked, muted, comfort in pools:
            result[mode]['_radio_pool'] = initial_pool(buckets, mode, blocked, muted, comfort)
        return result

    def positions(self, mode, keys):
        if self.generation is None or not keys:
            return {}
        reader = self.current_reader if self.current_reader is not None else self.store
        return {key: row['rank'] for key, row in reader.recommendations.ranks(
            self.surface('radio'), mode, 'ppr', keys, generation=self.generation).items()}

    def intensity_headings(self):
        if not self.has_intensity:
            return {}
        if self._intensity_headings is None:
            with self._reader() as reader:
                self._intensity_headings = reader.recommendations.intensity_headings(
                    self.surface('radio'), generation=self.generation)
        return self._intensity_headings

    def regions(self, mode, preference, genre_allowed, *, max_priority=None):
        """Order the immutable band headings before reading any member tracks."""
        bands = self.intensity_headings().get(mode, {})
        bounds = sorted((radio_intensity.band_priority(band, preference), band) for band in bands)
        for priority, band in bounds:
            if max_priority is not None and priority > max_priority:
                break
            check_cancellable_work()
            for label in bands[band]:
                if genre_allowed(label):
                    yield priority, band, label

    def intensity_pool(self, mode, preference, *, allowed, genre_allowed, excluded, used,
                       limit=LOOKAHEAD, max_priority=None):
        if type(limit) is not int or not 1 <= limit <= LOOKAHEAD:
            raise ValueError('Invalid Radio intensity lookahead')
        result, priority = [], None
        for tier, band, genre in self.regions(mode, preference, genre_allowed, max_priority=max_priority):
            if priority is not None and tier > priority:
                break
            found = self.tracks('radio', mode, 'membership', band=band, genres=[genre], allowed=allowed,
                                excluded=excluded, used=used, limit=limit - len(result), include_display=False)
            if found:
                priority = tier
                result.extend(found)
            if len(result) == limit:
                break
        return priority, result

    def contrast_witness(self, mode, preference, priority, witness, *,
                         contrast, allowed, genre_allowed, excluded, used):
        """Find a better live genre in the best fit tier using prepared region headings."""
        best = contrast(witness)
        regions = self.regions(mode, preference, genre_allowed, max_priority=priority)
        choices = sorted(((contrast({'genre': label}), band, label)
                          for tier, band, label in regions if priority == tier),
                         key=lambda choice: choice[0], reverse=True)
        for rank, band, label in choices:
            if rank <= best:
                break
            found = self.tracks('radio', mode, 'membership', band=band, genres=[label],
                                allowed=lambda track: allowed(track) and contrast(track) > best,
                                excluded=excluded, used=used, limit=1, include_display=False)
            if found:
                witness = found[0]
                best = contrast(witness)
        return witness
