"""Assemble current recap evidence without a reporting transaction."""
from yt_playlist.rec import recap_evidence as evidence


class Recap:
    def __init__(self, repository, genres):
        self.repository, self.genres = repository, genres

    def snapshot(self, since, until):
        plays = self.repository.plays(since, until, taste_filter=evidence.TASTE_FILTER,
                                      quarantine_group=evidence.QUARANTINE_GROUP)
        sources = self.repository.play_sources([row['id'] for row in plays])
        plays = [{**{key: value for key, value in row.items() if key != 'id'},
                  'sources': tuple(sorted(sources.get(row['id'], ())))} for row in plays]
        first = evidence.first_heard(self.repository.first_plays(
            until, taste_filter=evidence.TASTE_FILTER, quarantine_group=evidence.QUARANTINE_GROUP),
            self.repository.first_observations(until))
        meta = evidence.metadata(self.repository.track_metadata(), self.repository.track_albums(),
                                 self.repository.track_artists())
        axes = self.genres.feature_axes([card.get('genre') for card in meta.values()])
        for card in meta.values():
            card['family'] = axes[card['genre']][0] if card.get('genre') else ''
        playlists = self.repository.created_playlists(
            since - evidence.CALENDAR_MARGIN_SECONDS, until + evidence.CALENDAR_MARGIN_SECONDS,
            quarantine_group=evidence.QUARANTINE_GROUP, creation_sources=evidence.CREATION_SOURCES)
        return {'plays': plays, 'first': first, 'spans': self.repository.source_spans(), 'meta': meta,
                'familiar_artists': self.repository.familiar_artists(quarantine_group=evidence.QUARANTINE_GROUP),
                'artist_labels': self.repository.artist_labels(),
                'albums': evidence.albums(self.repository.full_album_tracks()),
                'playlists': evidence.playlist_dates(playlists, self.repository.calendars())}
