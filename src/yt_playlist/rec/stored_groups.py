"""Adaptive group choices over published ranks and immutable memberships."""
from collections import Counter, defaultdict
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.rec import artist_variety, group_roots, group_viz, home_vibe, mode_rules as mode_surfaces, musical_character, rec_params
from yt_playlist.rec.eligibility import comfort_tracks, home_exclusions
from yt_playlist.repos.group_ordering import GroupOrderingRepo


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


def preview_frame(reader, generation, version, lane, now, artist_cap, album_cap, muted, *, progress=None):
    progress = progress if progress is not None else PreviewFrame()
    if not progress.exhausted:
        check_cancellable_work()
        source = ('home', generation, 'all', lane)
        candidates, cursor = reader.recommendations.complete_candidates(
            [source], group_version=version, after=progress.after)
        # Finish the reads before advancing the frame.
        tracks = candidates.get(source, [])
        blocked = home_exclusions(reader, [track['key'] for track in tracks], now)
        preview_page(tracks, blocked, muted, artist_cap, album_cap, progress)
        progress.after, progress.exhausted = cursor, True
    tracks = progress.tracks + progress.reserve[:max(0, mode_surfaces.PROTO_SIZE - len(progress.tracks))]
    return {'eligible': max(progress.count, min(progress.available, mode_surfaces.PROTO_SIZE)),
            'full_mix': progress.available >= mode_surfaces.PROTO_SIZE, 'tracks': tracks}


@contextmanager
def selection(store, generation, now):
    close = (None if getattr(store, '_read_only', False) else
             group_roots.pending(store, {'recommendation_generations': [generation]}, now))
    try:
        if not getattr(store, '_read_only', False):
            with store.read_view() as reader:
                yield Selection(reader, generation, now)
        else:
            yield Selection(store, generation, now)
    finally:
        if close:
            close()


class Selection:
    def __init__(self, reader, generation, now, ordering=None):
        self.reader, self.generation, self.now, self.ordering = reader, generation, now, ordering
        self.preset = home_vibe.current(reader, now)
        self.muted = reader.rec_model.muted_artists()
        self.weights = reader.rec_model.get_weights(now=now)
        self.maximum = 60 * rec_params.get_param(reader, 'max_track_minutes')
        self.affinities = {}
        self.mix_candidates = None

    def eligible(self, page, lane, epoch, include, avoid, seen, meta, *, eligibility=None):
        if eligibility is None:
            blocked = home_exclusions(self.reader, [track['key'] for track in page], self.now)
            axes = self.reader.genres.feature_axes({track.get('genre') or '' for track in page})
        else:
            blocked, axes = eligibility
        eligible = eligible_page(page, lane, epoch, include, avoid, seen, meta, blocked=blocked,
            axes=axes, muted=self.muted, weights=self.weights, maximum=self.maximum)
        return comfort_tracks(self.reader, list(eligible), self.now) if lane == 'comfort' else eligible

    def tracks(self, version, lane, epoch, include, avoid, seen, artist_cap, album_cap, meta):
        check_cancellable_work()
        source = self.source(lane)
        candidates, _ = self.reader.recommendations.complete_candidates([source], group_version=version)
        eligible = list(self.eligible(candidates.get(source, ()), lane, epoch, include, avoid, seen, meta))
        limit = mode_surfaces.PROTO_SIZE * 3
        if self.preset:
            profiles = musical_character.load(self.reader, [track['key'] for track in eligible])
            with (nullcontext(self.ordering) if self.ordering is not None else GroupOrderingRepo.open()) as ordering:
                ordering.clear()
                ordering.add(affinity_rows(eligible, self.preset, profiles))
                rows = self.reader.artists.credited_rows(ordering.ranked())
                chosen = artist_variety.select(rows, artist_cap, album_cap, limit=limit)
            by_rank = {track['pool_rank']: track for track in eligible}
            tracks = [by_rank[row['rank']] for row in chosen]
            for track, row in zip(tracks, chosen, strict=True):
                track['serving_context']['vibe_affinity'] = row['fit']
        else:
            tracks = artist_variety.select(eligible, artist_cap, album_cap, limit=limit)
            self.default_affinity(tracks)
        self.reader.recommendations.explain_candidates(source, tracks)
        return tracks

    def source(self, lane):
        return 'home', self.generation, 'all', lane

    def load_lanes(self, group_generation, lanes, mix_candidates=None, memberships=None, *, matching_vibe=True):
        slots = [self.source(lane) for lane in lanes]
        if self.preset:
            mix_candidates = self.reader.recommendations.mix_candidates(
                slots, (), vibe=self.preset, matching_vibe=matching_vibe)
            self.mix_candidates = mix_candidates
            self.affinities.update((track['key'], home_vibe.score_or_neutral(track['vibe_fit']))
                                   for track in mix_candidates.facts.values())
        ranks = (mix_candidates.candidates(slots) if mix_candidates is not None
                 else self.reader.recommendations.complete_candidates(slots)[0])
        if memberships is None:
            memberships = (group_viz.recording_groups(self.reader, group_generation) if self.preset and ranks else
                           self.reader.taste_groups.recording_groups(group_generation) if ranks else {})
        return ranks, memberships

    def lane_eligibility(self, ranks):
        """Share current exclusions and genre lookups across the ranked lanes."""
        check_cancellable_work()
        if not ranks:
            return set(), {}
        blocked = home_exclusions(self.reader, None, self.now, check_genres=not self.preset)
        axes = self.reader.genres.feature_axes({track.get('genre') or ''
                                               for tracks in ranks.values() for track in tracks})
        return blocked, axes

    def lane_groups(self, ranks, memberships, lane, epoch, include, avoid, seen, meta, *, eligibility):
        grouped = defaultdict(list)
        page = ranks.get(self.source(lane), ())
        for track in self.eligible(page, lane, epoch, include, avoid, seen, meta, eligibility=eligibility):
            for version in memberships.get(track['recording_id'], ()):
                grouped[version].append(track)
        return grouped

    @staticmethod
    def default_affinity(tracks):
        for track in tracks:
            track['serving_context']['vibe_affinity'] = .5

    def vibe_tracks(self, members, artist_cap, album_cap):
        """Rank group members with the scores read alongside the published ranks."""
        def priority(track):
            fit = self.affinities[track['key']]
            return (-round((fit - .5) / home_vibe.FIT_STEP), -home_vibe.taste_score(track),
                    -fit, track['pool_rank'])

        return artist_variety.select(sorted(members, key=priority), artist_cap, album_cap,
                                     limit=mode_surfaces.PROTO_SIZE * 3)

    def hydrate(self, cards):
        tracks = [track for card in cards for track in card['tracks']]
        self.reader.recommendations.explain_mix(tracks)
        unavailable = self.mix_candidates.load_display(tracks)
        for card in cards:
            card['tracks'] = [track for track in card['tracks'] if track['recording_id'] not in unavailable]
            for track in card['tracks']:
                track['serving_context']['vibe_affinity'] = self.affinities[track['key']]


def eligible_page(page, lane, epoch, include, avoid, seen, meta, *, blocked, axes, muted, weights, maximum):
    """Apply lane and playback eligibility to prepared candidates."""
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
    for track in tracks:
        artist, album = track.get('artist') or '', track.get('album') or ''
        if track['key'] in blocked or artist in muted:
            continue
        # Availability is not reduced by a soft variety preference.
        progress.available += 1
        members = artist_variety.members(track)
        if (any(progress.artists[member] >= artist_cap for member in members)
                or album and progress.albums[album] >= album_cap):
            if len(progress.reserve) < mode_surfaces.PROTO_SIZE:
                selected = {field: track.get(field) for field in ('key', 'title', 'artist')}
                progress.reserve.append(selected)
            continue
        progress.count += 1
        progress.artists.update(members)
        if album:
            progress.albums[album] += 1
        if len(progress.tracks) < mode_surfaces.PROTO_SIZE:
            selected = {field: track.get(field) for field in ('key', 'title', 'artist')}
            progress.tracks.append(selected)
