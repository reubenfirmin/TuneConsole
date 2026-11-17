"""Discovery page facts, visible impressions and collection acquisition."""


class DiscoveryActions:
    def __init__(self, discovery, *, retention_days, account, rating, logger):
        self.discovery = discovery
        self.retention_days, self.account, self.rating, self.logger = retention_days, account, rating, logger

    def page(self, now):
        days = self.retention_days()
        return {'gc_days': days, 'now': now,
                'tracks': self.discovery.discovery_track_view(now, days),
                'albums': self.discovery.discovery_album_view(now, days),
                'artists': self.discovery.discovery_artist_view(now, days)}

    def impressions(self, kind, ids, view_id, now):
        self.discovery.record_discovery_impressions(kind, ids, view_id, now)

    def acquire(self, kind, identity):
        try:
            self.account()
        except ValueError as exc:
            return {'status': 'unavailable', 'message': str(exc)}
        if kind != 'track':
            return {'status': 'confirmed', 'message': ''}
        row = self.discovery.discovered_tracks_by_keys([identity]).get(identity)
        video = row.get('video_id') if row else None
        if not video:
            return {'status': 'missing_video', 'message': 'This track needs a YouTube video before it can be saved.'}
        try:
            result = self.rating(video, 'LIKE', acquisition={'title': row['title'], 'artist': row['artist'],
                'album': row.get('album'), 'identity_key': identity})
        except Exception:
            self.logger.warning('discovery add (like) failed for %s', identity, exc_info=True)
            return {'status': 'unconfirmed', 'message': "Couldn't save this track to Liked Music. Try again."}
        return {'status': 'confirmed', 'message': result.get('player_error') or ''}
