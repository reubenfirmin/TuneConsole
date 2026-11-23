"""Editorial demand and publication, independent of SQL and file storage details."""
from yt_playlist.core import album_note_outputs, album_note_requests
from yt_playlist.core.recommendation_work import finite


class AlbumNotes:
    EDITORIAL_VERSION = 7
    VISIBLE_SECONDS = 900
    REQUEST_LIMIT = 24
    def __init__(self, repository, proposals, *, paused):
        self.repository = repository
        self.proposals = proposals
        self.paused = paused

    def _demand(self, albums, cache, queued, now, current_only, visible):
        albums = tuple(albums)
        if current_only:
            current = {album['browse_id'] for album in albums}
            queued = {browse: value for browse, value in queued.items()
                      if browse in current or value.get('requested_until', 0) > now}
        for album in albums:
            browse = album['browse_id']
            cached = cache.get(browse, {})
            if (not album.get('artist') or not album.get('title')
                    or cached.get('editorial_version') == self.EDITORIAL_VERSION and cached.get('retry_at', 0) > now):
                continue
            until = queued.get(browse, {}).get('requested_until')
            if visible:
                until = max(until if until is not None else now, now + self.VISIBLE_SECONDS)
            value = {key: album.get(key) for key in ('browse_id', 'artist', 'title', 'year', 'thumbnail')}
            if until is not None:
                value['requested_until'] = until
            queued[browse] = value
        return dict(list(queued.items())[-self.REQUEST_LIMIT:])

    def request(self, albums, now, *, current_only=False, visible=False):
        if self.paused():
            return
        cache = self.proposals.get_proposals('album_notes') or {}
        finite(now)
        with self.repository.transaction():
            previous = self.repository.requests()
            queued = self._demand(albums, cache, dict(previous), now, current_only, visible)
            records = album_note_requests.parse(queued, now)
            for browse in previous.keys() - queued.keys():
                self.repository.remove_request(browse)
            for fields in records:
                self.repository.store_request(fields)

    def publish(self, browse, note, now, *, expected_request=None):
        if self.paused():
            return False
        if now is not None:
            finite(now)
        return self.repository.publish_output(browse, album_note_outputs.encode(browse, note),
                                              expected_request=expected_request)

    def next_request(self):
        return self.repository.next_request()

    def wake(self, now):
        notes = self.proposals.get_proposals('album_notes') or {}
        for note in notes.values():
            if (note.get('description') or {}).get('kind') != 'generated':
                note['retry_at'] = 0
        self.proposals.put_proposals('album_notes', notes, now)
