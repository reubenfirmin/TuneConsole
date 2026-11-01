"""A live Radio reader over one retained, immutable recommendation publication."""
from contextlib import nullcontext
from itertools import zip_longest
import math
import weakref

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import group_roots, musical_character, radio_intensity

PAGE_SIZE = 128
LOOKAHEAD = 96


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
        if generation is None:
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

    def _reader(self):
        return nullcontext(self.store) if getattr(self.store, '_read_only', False) else self.store.read_view()

    def tracks(self, surface, mode, purpose, *, allowed, excluded=(), used=(), limit=LOOKAHEAD,
               band=None, genres=None, reader=None):
        """Advance past unavailable and rejected rows without a global cutoff."""
        if type(limit) is not int or not 1 <= limit <= LOOKAHEAD:
            raise ValueError('Invalid Radio lookahead limit')
        if self.generation is None:
            return []
        result, after = [], -1
        with (nullcontext(reader) if reader is not None else self._reader()) as current:
            while len(result) < limit:
                check_cancellable_work()
                page = current.recommendations.candidate_page(self.surface(surface), mode, purpose, generation=self.generation,
                    after=after, limit=PAGE_SIZE, band=band, genres=genres)
                after = page['after']
                for track in page['tracks']:
                    if track['key'] in used or track['video_id'] in excluded or not allowed(track):
                        continue
                    if surface == 'radio' and purpose == 'membership':
                        track['_radio_catalog_score'] = track.get('score') or 0
                    result.append(track)
                    if len(result) == limit:
                        break
                if page['exhausted']:
                    break
        return result

    def inputs(self, reader, modes, space, model_generation, blocked, muted, comfort):
        """Hydrate only the initial live lookahead, keeping membership in SQLite."""
        if self.generation is None:
            return {}
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
        for mode in modes:
            buckets = {lane: self.tracks('home', mode, lane, reader=reader, allowed=allowed)
                       for lane in ('wheelhouse', 'explore', 'comfort', 'temporal', 'fresh')}
            result[mode] = {'_radio_pool': initial_pool(buckets, mode, blocked, muted, comfort)}
        if not any(value['_radio_pool'] for mode, value in result.items() if mode in modes) and not any(members.values()):
            result['all'] = {lane: self.tracks('home', 'all', lane, reader=reader, allowed=allowed)
                             for lane in ('wheelhouse', 'fresh')}
            if not result['all']['fresh']:
                result['all']['fresh'] = self.tracks('fresh', 'all', 'fresh', reader=reader, allowed=allowed)
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
            bands = repo.intensity_bands(self.surface('radio'), mode, 'membership', generation=self.generation)
            bands.sort(key=lambda band: (radio_intensity.loss(band / radio_intensity.RESOLUTION, energy),
                                         abs(band / radio_intensity.RESOLUTION - energy)))
            for band in bands:
                after = None
                while True:
                    check_cancellable_work()
                    headings = repo.band_page(self.surface('radio'), mode, 'membership', generation=self.generation,
                                             band=band, after=after, limit=PAGE_SIZE)
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
                                excluded=excluded, used=used, limit=limit - len(result))
            if found:
                priority = loss
                result.extend(found)
            if len(result) == limit:
                break
        return priority, result
