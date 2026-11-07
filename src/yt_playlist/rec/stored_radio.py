"""A live Radio reader over one retained, immutable recommendation publication."""
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from itertools import zip_longest
import math
import weakref

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import group_roots, musical_character, radio_intensity

PAGE_SIZE = 128
LOOKAHEAD = 96
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
    names = ('wheelhouse', 'explore', 'comfort', 'temporal', 'fresh')
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
        if not getattr(store, '_read_only', False) and store.activity_paused:
            # Maintenance keeps the current publication immutable. Select it
            # without writing a lease; the live session retains it through the
            # existing resume callback before recommendation workers restart.
            with store.read_view() as reader:
                selected, _ = group_roots.pending_head(reader,
                    tuple(self.surface(name) for name in ('radio', 'home', 'fresh')), at)
            self.generation, close = generation if generation is not None else selected, None
        elif generation is None:
            self.generation, close = group_roots.pending_head(store, tuple(self.surface(name) for name in ('radio', 'home', 'fresh')), at)
        else:
            self.generation = generation
            close = None if getattr(store, '_read_only', False) else group_roots.pending(
                store, {'recommendation_generations': [generation]}, at)
        self._lifetime = weakref.finalize(self, close) if close is not None else None
        self.has_intensity = False

    def surface(self, name):
        return self.surface_prefix + name

    def close(self):
        if self._lifetime is not None:
            self._lifetime()

    def retain(self, at):
        """Retain a maintenance-started session before background work resumes."""
        if (self._lifetime is None and self.generation is not None
                and not getattr(self.store, '_read_only', False) and not self.store.activity_paused):
            close = group_roots.pending(self.store, {'recommendation_generations': [self.generation]}, at)
            if close is not None:
                self._lifetime = weakref.finalize(self, close)

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

    def tracks(self, surface, mode, purpose, *, allowed, excluded=(), used=(), limit=LOOKAHEAD,
               band=None, genres=None, reader=None, include_display=True):
        """Advance past unavailable and rejected rows without a global cutoff."""
        if type(limit) is not int or not 1 <= limit <= LOOKAHEAD:
            raise ValueError('Invalid Radio lookahead limit')
        if self.generation is None:
            return []
        result, after = [], -1
        with (nullcontext(reader) if reader is not None else self._reader()) as current:
            while len(result) < limit:
                check_cancellable_work()
                key = (surface, mode, purpose, after, band, tuple(genres) if genres is not None else None,
                       include_display)
                page = self._cached_read('pages', key, lambda: current.recommendations.candidate_page(
                    self.surface(surface), mode, purpose, generation=self.generation, after=after,
                    limit=PAGE_SIZE, band=band, genres=genres, include_display=include_display), 256)
                after = page['after']
                for track in page['tracks']:
                    if track['key'] in used or track['video_id'] in excluded or not allowed(track):
                        continue
                    # Issued choices gain mode/profile metadata. Never let that
                    # mutate a cached catalog page used by another branch.
                    track = deepcopy(track)
                    if surface == 'radio' and purpose == 'membership':
                        track['_radio_catalog_score'] = track.get('score') or 0
                    result.append(track)
                    if len(result) == limit:
                        break
                if page['exhausted']:
                    break
        return result

    def capture_inputs(self, reader, modes, space, model_generation, blocked, muted, comfort):
        """Capture the live lookahead and file owners from one pinned SQL view."""
        if self.generation is None:
            return {}, [], []
        repo = reader.recommendations
        framing = repo.framing(self.surface('home'), generation=self.generation) or repo.framing(self.surface('radio'), generation=self.generation) or {}
        valid = framing.get('radio_space') == space and (bool(self.surface_prefix) or framing.get('radio_generation') == model_generation)
        members = {row['mode_key']: row['eligible_count'] for row in
                   repo.selections(self.surface('radio'), 'membership', generation=self.generation)} if valid else {}
        self.has_intensity = bool(members and framing.get('intensity_version') == radio_intensity.VERSION
                                  and framing.get('radio_profile_version') == musical_character.VERSION)
        result = {'_meta': {'musical_generation': framing.get('musical_generation')},
                  '_radio': {'generation': framing.get('radio_generation'), 'space': framing.get('radio_space'),
                             'members': members}}
        allowed = lambda track: track['key'] not in blocked and (track.get('artist') or '').casefold() not in muted
        displays, pools = [], []

        # Lanes share many recordings. Hydrate their bounded pages together,
        # rather than repeating current catalog/genre reads for every lane.
        lanes = ('wheelhouse', 'explore', 'comfort', 'temporal', 'fresh')
        selected = {(mode, lane): [] for mode in modes for lane in lanes}
        pending = [(mode, lane, -1) for mode, lane in selected]
        while pending:
            remaining = []
            for offset in range(0, len(pending), 16):
                check_cancellable_work()
                batch = pending[offset:offset + 16]
                pages = repo.candidate_pages(self.surface('home'), batch, generation=self.generation,
                                             limit=PAGE_SIZE, include_display=False)
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

        def tracks(surface, mode, lane):
            rows = (selected[mode, lane] if surface == 'home' and (mode, lane) in selected else
                    self.tracks(surface, mode, lane, reader=reader, allowed=allowed, include_display=False))
            ranks = [track['pool_rank'] for track in rows]
            chunks = repo.display_references(self.surface(surface), mode, lane, ranks, generation=self.generation)
            displays.append((self.surface(surface), rows, ranks, chunks))
            return rows

        for mode in modes:
            buckets = {lane: tracks('home', mode, lane) for lane in lanes}
            result[mode] = {'_radio_pool': initial_pool(buckets, mode, blocked, muted, comfort)}
            pools.append((mode, buckets, blocked, muted, comfort))
        if not any(value['_radio_pool'] for mode, value in result.items() if mode in modes) and not any(members.values()):
            result['all'] = {lane: tracks('home', 'all', lane)
                             for lane in ('wheelhouse', 'fresh')}
            if not result['all']['fresh']:
                result['all']['fresh'] = tracks('fresh', 'all', 'fresh')
        return result, displays, pools

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
        if self.generation is None:
            return {}
        return {key: row['rank'] for key, row in self.store.recommendations.ranks(
            self.surface('radio'), mode, 'ppr', keys, generation=self.generation).items()}

    def regions(self, mode, energy, genre_allowed):
        """Order the small intensity headings; page genres inside each band."""
        if self.generation is None:
            return
        with self._reader() as reader:
            repo = reader.recommendations
            bands = self._cached_read('headings', ('bands', mode), lambda: repo.intensity_bands(
                self.surface('radio'), mode, 'membership', generation=self.generation), 128)
            bands = sorted(bands, key=lambda band: (radio_intensity.loss(band / radio_intensity.RESOLUTION, energy),
                                                   abs(band / radio_intensity.RESOLUTION - energy)))
            for band in bands:
                after = None
                while True:
                    check_cancellable_work()
                    headings = self._cached_read('headings', ('genres', mode, band, after),
                        lambda: repo.band_page(self.surface('radio'), mode, 'membership',
                            generation=self.generation, band=band, after=after, limit=PAGE_SIZE), 128)
                    for row in headings:
                        if genre_allowed(row['genre_key']):
                            yield radio_intensity.loss(band / radio_intensity.RESOLUTION, energy), band, row['genre_key']
                    if len(headings) < PAGE_SIZE:
                        break
                    after = (band, headings[-1]['genre_key'])

    def intensity_pool(self, mode, energy, *, allowed, genre_allowed, excluded, used, limit=LOOKAHEAD):
        if (type(limit) is not int or not 1 <= limit <= LOOKAHEAD or type(energy) not in (int, float)
                or not math.isfinite(energy) or not 0 <= energy <= 1):
            raise ValueError('Invalid Radio intensity lookahead')
        result, priority = [], None
        for loss, band, genre in self.regions(mode, energy, genre_allowed):
            if priority is not None and loss > priority:
                break
            found = self.tracks('radio', mode, 'membership', band=band, genres=[genre], allowed=allowed,
                                excluded=excluded, used=used, limit=limit - len(result), include_display=False)
            if found:
                priority = loss
                result.extend(found)
            if len(result) == limit:
                break
        return priority, result
