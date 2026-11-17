"""Adaptive group choices over indexed immutable memberships and bounded pages."""
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import artist_variety, group_roots, home_vibe, mode_rules as mode_surfaces, musical_character, rec_params
from yt_playlist.rec.eligibility import home_exclusions
from yt_playlist.repos.group_ordering import GroupOrderingRepo


def pages(reader, generation, version, lane):
    after = -1
    while True:
        check_cancellable_work()
        page = reader.recommendations.candidate_page('home', 'all', lane, generation=generation,
            group_version=version, after=after, limit=128)
        yield page['tracks']
        after = page['after']
        if page['exhausted']:
            break


@dataclass
class PreviewFrame:
    after: int = -1
    artists: Counter = field(default_factory=Counter)
    albums: Counter = field(default_factory=Counter)
    count: int = 0
    available: int = 0
    tracks: list = field(default_factory=list)
    reserve: list = field(default_factory=list)
    exhausted: bool = False


def restore_preview_pages(pages):
    frames = {}
    for version, lane, delta in pages:
        key = version, lane
        if 'legacy' in delta:
            frames[key] = delta['legacy']
            continue
        state = frames.setdefault(key, PreviewFrame())
        state.after, state.exhausted = delta['after'], delta['exhausted']
        state.artists.update(delta['artists'])
        state.albums.update(delta['albums'])
        state.count += delta['count']
        state.available += delta.get('available', delta['count'])
        state.tracks.extend(delta['tracks'])
        state.reserve.extend(delta.get('reserve', ()))
    return frames


def preview_frame(reader, generation, version, lane, now, artist_cap, album_cap, muted, *, progress=None,
                  checkpoint=None):
    progress = progress if progress is not None else PreviewFrame()
    while not progress.exhausted:
        check_cancellable_work()
        page = reader.recommendations.candidate_page('home', 'all', lane, generation=generation,
            group_version=version, after=progress.after, limit=128)
        # Finish all interruptible reads before advancing the page. A cancelled
        # page is retried; completed pages retain counts, caps and ordered songs.
        tracks = page['tracks']
        blocked = home_exclusions(reader, [track['key'] for track in tracks], now)
        after = progress.after
        delta = preview_page(tracks, blocked, muted, artist_cap, album_cap, progress)
        progress.after, progress.exhausted = page['after'], page['exhausted']
        if checkpoint is not None:
            checkpoint(version, lane, after, {**delta, 'after': progress.after, 'exhausted': progress.exhausted})
    tracks = progress.tracks + progress.reserve[:max(0, mode_surfaces.PROTO_SIZE - len(progress.tracks))]
    return {'eligible': max(progress.count, min(progress.available, mode_surfaces.PROTO_SIZE)),
            'full_mix': progress.available >= mode_surfaces.PROTO_SIZE, 'tracks': tracks}


@contextmanager
def selection(store, generation, now):
    close = (None if getattr(store, '_read_only', False) else
             group_roots.pending(store, {'recommendation_generations': [generation]}, now))
    try:
        if not getattr(store, '_read_only', False):
            with store.read_view() as reader, selection_reader(reader, generation, now) as selected:
                yield selected
        else:
            with selection_reader(store, generation, now) as selected:
                yield selected
    finally:
        if close:
            close()


@contextmanager
def selection_reader(reader, generation, now):
    with GroupOrderingRepo.open() as ordering:
        yield Selection(reader, generation, now, ordering)


class Selection:
    def __init__(self, reader, generation, now, ordering):
        self.reader, self.generation, self.now, self.ordering = reader, generation, now, ordering
        self.preset = home_vibe.current(reader, now)
        self.muted = reader.rec_model.muted_artists()
        self.weights = reader.rec_model.get_weights(now=now)
        self.maximum = 60 * rec_params.get_param(reader, 'max_track_minutes')

    def eligible(self, page, lane, epoch, include, avoid, seen, meta):
        blocked = home_exclusions(self.reader, [track['key'] for track in page], self.now)
        axes = self.reader.genres.feature_axes({track.get('genre') or '' for track in page})
        return eligible_page(page, lane, epoch, include, avoid, seen, meta, blocked=blocked,
            axes=axes, muted=self.muted, weights=self.weights, maximum=self.maximum)

    def tracks(self, version, lane, epoch, include, avoid, seen, artist_cap, album_cap, meta):
        limit = mode_surfaces.PROTO_SIZE * 3
        selected = artist_variety.Selection(artist_cap, album_cap, limit=limit)

        self.ordering.clear()
        for page in pages(self.reader, self.generation, version, lane):
            eligible = list(self.eligible(page, lane, epoch, include, avoid, seen, meta))
            if not self.preset:
                for track in eligible:
                    track['serving_context']['vibe_affinity'] = .5
                    if selected.add(track):
                        return selected.finish()
            else:
                # A page owns its profile cache. The full ranking keeps only
                # compact sort keys in SQLite and can spill to disk.
                profiles = musical_character.load(self.reader, [track['key'] for track in eligible])
                self.ordering.add(affinity_rows(eligible, self.preset, profiles))
        if not self.preset:
            return selected.finish()
        rows = self.reader.artists.credited_pages(self.ordering.ranked())
        chosen = artist_variety.select(rows, artist_cap, album_cap, limit=limit)
        tracks = []
        for row in chosen:
            track = self.reader.recommendations.candidate_page('home', 'all', lane,
                generation=self.generation, after=row['rank'] - 1, limit=1)['tracks'][0]
            track['serving_context']['vibe_affinity'] = row['fit']
            tracks.append(track)
        return tracks


def eligible_page(page, lane, epoch, include, avoid, seen, meta, *, blocked, axes, muted, weights, maximum):
    """Select one detached page; the controller retains bounded reads and early exit."""
    for track in page:
        genre = track.get('genre') or ''
        family, subgenre = axes.get(genre, ('', None))
        if (track.get('_radio_fresh') or track['key'] in seen or track['key'] in blocked
                or track.get('artist') in muted
                or weights.get(f'genre:{family}', 1) == 0
                or weights.get(f'genre:{subgenre}', 1) == 0
                or track.get('duration') and track['duration'] > maximum
                or (include or avoid) and (not genre or family in avoid or include and family not in include)):
            continue
        if lane == 'temporal':
            cuts = meta.get('year_cuts')
            if not cuts or not mode_surfaces._in_band(track.get('year'), epoch % 3, *cuts):
                continue
        yield track


def affinity_rows(eligible, preset, profiles):
    rows = []
    for track in eligible:
        fit = home_vibe.affinity(track, preset, profiles)
        rows.append((track['pool_rank'], track['key'], track.get('artist') or '',
                     track.get('album') or '', -round((fit - .5) / home_vibe.FIT_STEP),
                     -home_vibe.taste_score(track), fit))
    return rows


def preview_page(tracks, blocked, muted, artist_cap, album_cap, progress):
    delta = {'artists': Counter(), 'albums': Counter(), 'count': 0, 'available': 0, 'tracks': [], 'reserve': []}
    for track in tracks:
        artist, album = track.get('artist') or '', track.get('album') or ''
        if track['key'] in blocked or artist in muted:
            continue
        # Availability is not reduced by a soft variety preference.
        progress.available += 1
        delta['available'] += 1
        members = artist_variety.members(track)
        if (any(progress.artists[member] >= artist_cap for member in members)
                or album and progress.albums[album] >= album_cap):
            if len(progress.reserve) < mode_surfaces.PROTO_SIZE:
                selected = {field: track.get(field) for field in ('key', 'title', 'artist')}
                progress.reserve.append(selected)
                delta['reserve'].append(selected)
            continue
        progress.count += 1
        delta['count'] += 1
        progress.artists.update(members)
        delta['artists'].update(members)
        if album:
            progress.albums[album] += 1
            delta['albums'][album] += 1
        if len(progress.tracks) < mode_surfaces.PROTO_SIZE:
            selected = {field: track.get(field) for field in ('key', 'title', 'artist')}
            progress.tracks.append(selected)
            delta['tracks'].append(selected)
    return delta
