"""Bounded, coherent Radio feedback and taxonomy acquisition."""
from contextlib import nullcontext
from itertools import islice

from yt_playlist.core.activity import check_cancellable_work


class RadioInputs:
    def __init__(self, read_view):
        self.read_view = read_view

    def feedback(self, started_at, now):
        with self.read_view() as reader:
            return {'likes': reader.rec_model.recent_liked_with_ts(limit=200),
                'dislikes': reader.rec_model.disliked_with_ts(),
                'exits': reader.player_events.player_events_since(max(started_at, now - 45), kind='track_exit'),
                'blocked': reader.rec_model.disliked_identity_keys() | reader.rec_model.suppressed_keys('for_you', now),
                'muted': {artist.casefold() for artist in reader.rec_model.muted_artists()}}

    def taxonomy(self, labels=()):
        with self.read_view() as reader:
            return self.taxonomy_facts(reader, labels)

    @staticmethod
    def taxonomy_facts(reader, labels=()):
        axes = reader.genres.feature_axes([*reader.genres.all_genres(), *reader.genres.get_genre_whitelist(), *labels])
        keys = [key for _, key in axes.values() if key]
        return {**reader.genres.feature_axes(keys), **axes}

    @staticmethod
    def exclusions(reader, now):
        return (reader.rec_model.disliked_identity_keys() | reader.rec_model.suppressed_keys('for_you', now),
                {artist.casefold() for artist in reader.rec_model.muted_artists()})


def inputs_for(store):
    def read():
        owned = getattr(store, '_read_only', False) or getattr(store._lock, 'held_by_current_thread', False)
        return nullcontext(store) if owned else store.read_view()
    return RadioInputs(read)


class RadioCatalog:
    """Bounded catalog acquisition beneath the existing session/planning owner."""
    def __init__(self, repository):
        self.repository = repository

    def pages(self, read, allowed, excluded, excluded_keys, limit, *, short_page_ends=False):
        from yt_playlist.rec.radio_ranking import eligible_page
        tracks, seen, after = [], set(excluded_keys), 0
        while len(tracks) < limit:
            check_cancellable_work()
            rows = read(after, limit)
            check_cancellable_work()
            if not rows:
                break
            after = rows[-1]['id']
            chosen = eligible_page(rows, seen, excluded, allowed, limit - len(tracks))
            tracks.extend(chosen)
            seen.update(track['key'] for track in chosen)
            if short_page_ends and len(rows) < limit:
                break
        return tracks

    def fallback(self, allowed, *, limit):
        return self.pages(lambda after, size: self.repository.fallback_tracks(size, after=after),
                          allowed, (), (), limit, short_page_ends=True)

    def cluster(self, keys, excluded, excluded_keys, allowed, *, limit):
        from yt_playlist.rec.radio_ranking import eligible_page
        tracks, seen = [], set(excluded_keys)
        remaining = (key for key in keys if key not in seen)
        while batch := list(islice(remaining, limit)):
            check_cancellable_work()
            rows = self.repository.cluster_tracks(batch)
            chosen = eligible_page(rows, seen, excluded, allowed, limit - len(tracks))
            tracks.extend(chosen)
            seen.update(track['key'] for track in chosen)
            if len(tracks) == limit:
                break
        return tracks

    def themed(self, genres, excluded, excluded_keys, allowed, *, limit):
        if not genres.pinned or not (genres.current or {}).get('genre'):
            return []
        labels = [label for label in self.repository.genre_labels() if genres.allows({'genre': label})]
        if not labels:
            return []
        return self.pages(lambda after, size: self.repository.genre_tracks(labels, after, size),
                          allowed, excluded, excluded_keys, limit)
