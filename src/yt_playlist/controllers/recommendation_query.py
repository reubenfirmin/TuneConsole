"""Recommendation catalog features composed from repository facts."""
from collections import defaultdict

from yt_playlist.core.artists import token
from yt_playlist.rec import catalog_features
from yt_playlist.repos.rec_query import RecQueryRepo
from yt_playlist.util.years import decade


class RecommendationQuery:
    AUDIO_COLS = RecQueryRepo.AUDIO_COLS

    def __init__(self, repository, genres, eligibility, artists):
        self.repository, self.genres = repository, genres
        self.eligibility, self.artists = eligibility, artists

    def artist_genres(self, *, canonical=False):
        rows = self.repository.artist_genre_counts()
        names = {row['artist'] for row in rows}
        ambiguous = {name for name in names if self.artists.resolve(name) is None} if not canonical else names
        best = {}
        for row in rows:
            artist = token(row['artist_id']) if canonical or row['artist'] in ambiguous else row['artist']
            if artist not in best or row['c'] > best[artist][1]:
                best[artist] = row['genre'], row['c']
        return {artist: genre for artist, (genre, _) in best.items()}

    def artist_genre_years_for(self, names, *, per_artist=64):
        return {name: catalog_features.artist_features(rows) for name, rows in
                self.repository.artist_metadata_for(names, per_artist=per_artist).items()}

    def artist_genre_years(self):
        by_artist = defaultdict(list)
        for row in self.repository.artist_metadata():
            by_artist[row['artist']].append(row)
        return {name: features for name, rows in by_artist.items()
                if (features := catalog_features.artist_features(rows))['genre'] or features['year'] is not None}

    def era_play_distribution(self):
        return catalog_features.era_weights(
            self.repository.year_play_weights(owned_weight=catalog_features.OWNED_WEIGHT))

    def genre_play_distribution(self):
        return self.repository.genre_play_distribution(owned_weight=catalog_features.OWNED_WEIGHT)

    def play_weighted_facets(self, plays):
        return catalog_features.play_weighted_facets(self.repository.tagged_facets(), plays)

    def library_genre_families(self):
        counts = self.repository.library_genre_counts()
        axes = self.genres.feature_axes([row['genre'] for row in counts])
        families = defaultdict(int)
        for row in counts:
            families[axes[row['genre']][0]] += row['n']
        return [{'family': family, 'n': count} for family, count in
                sorted(families.items(), key=lambda item: (-item[1], item[0]))]

    def library_genres(self):
        counts = self.repository.library_genre_counts()
        axes = self.genres.feature_axes([row['genre'] for row in counts])
        return [{'genre': row['genre'], 'family': axes[row['genre']][0], 'n': row['n']}
                for row in sorted(counts, key=lambda row: (-row['n'], row['genre'].lower()))]

    def keys_in_genre_selection(self, tokens):
        tokens = list(tokens)
        normalized = {value.strip().lower() for value in tokens if value and value.strip()}
        if not normalized:
            return set()
        labels = [row['genre'] for row in self.repository.library_genre_counts()]
        axes = self.genres.feature_axes([*labels, *tokens])
        keys = {axes[value][1] for value in tokens if value in axes and axes[value][1] is not None}
        selected = [label for label in labels if label.strip().lower() in normalized
                    or axes[label][1] in keys or axes[label][0].lower() in normalized]
        return self.repository.keys_for_genres(selected)

    def recipe_facet_keys(self):
        rows = self.repository.facet_values()
        genres = list(dict.fromkeys(row['genre'] for row in rows if row['genre']))
        axes = self.genres.feature_axes(genres)
        return (dict.fromkeys((axes[genre][0] for genre in genres), 1),
                dict.fromkeys((era for row in rows
                               if (era := decade(row['mb_year'], as_str=True)) is not None), 1))

    def library_songs(self):
        return self.repository.library_songs(maximum_duration=self.eligibility.maximum_duration())

    def key_for_video(self, video_id):
        return self.repository.key_for_video(video_id)

    def track_genres(self, keys):
        return self.repository.track_genres(keys)

    def track_decades(self, keys):
        return self.repository.track_decades(keys)

    def track_last_played(self, keys):
        return self.repository.track_last_played(keys)

    def track_artists(self, keys):
        return self.repository.track_artists(keys)

    def track_popularity(self, keys):
        return self.repository.track_popularity(keys)

    def tracks_total(self):
        return self.repository.tracks_total()

    def owned_albums(self):
        return self.repository.owned_albums()

    def library_keys(self):
        return self.repository.library_keys()

    def library_artists(self):
        return self.repository.library_artists()

    def tracks_by_artists(self, artists):
        return self.repository.tracks_by_artists(artists)

    def track_content(self):
        return self.repository.track_content()

    def content_inputs_by_keys(self, keys):
        return self.repository.content_inputs_by_keys(keys)

    def track_audio_features(self):
        return self.repository.track_audio_features()

    def tracks_by_keys(self, keys):
        return self.repository.tracks_by_keys(keys)

    def group_candidates(self, keys):
        return self.repository.group_candidates(keys)

    def top_played_keys(self, limit=10):
        return self.repository.top_played_keys(limit)

    def play_counts(self):
        return self.repository.play_counts()

    def genre_distribution(self):
        return self.repository.genre_distribution()

    def playlist_track_genres(self, playlist_id):
        return self.repository.playlist_track_genres(playlist_id)
