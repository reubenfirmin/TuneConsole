"""Editorial demand and publication, independent of SQL and file storage details."""
from yt_playlist.core import album_note_requests, recommendation_outputs
from yt_playlist.core.recommendation_work import finite


class AlbumNotes:
    EDITORIAL_VERSION = 7
    VISIBLE_SECONDS = 900
    REQUEST_LIMIT = 24
    NOTE_LIMIT = 128

    def __init__(self, repository, proposals, files, *, installed, paused):
        self.repository = repository
        self.proposals = proposals
        self.files = files
        self.installed = installed
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
        if not self.installed:
            queued = self.proposals.get_proposals('album_note_requests') or {}
            queued = self._demand(albums, cache, queued, now, current_only, visible)
            self.proposals.put_proposals('album_note_requests', queued, now)
            return
        finite(now)
        with self.repository.transaction():
            previous = self.repository.requests()
            queued = self._demand(albums, cache, dict(previous), now, current_only, visible)
            records = album_note_requests.parse(queued, now)
            for browse in previous.keys() - queued.keys():
                self.repository.remove_request(browse)
            for fields in records:
                value = queued[fields[0]]
                before = previous.get(fields[0])
                changed_work = (before is None or
                    {key: item for key, item in before.items() if key != 'requested_until'} !=
                    {key: item for key, item in value.items() if key != 'requested_until'})
                self.repository.store_request(fields, now, changed_work=changed_work)

    def publish(self, browse, note, now, *, expected_request=None):
        if self.paused():
            return False
        if not self.installed:
            cache = self.proposals.get_proposals('album_notes') or {}
            cache.pop(browse, None)
            cache[browse] = note
            self.proposals.put_proposals('album_notes', dict(list(cache.items())[-self.NOTE_LIMIT:]), now)
            queued = self.proposals.get_proposals('album_note_requests') or {}
            queued.pop(browse, None)
            self.proposals.put_proposals('album_note_requests', queued, now)
            return
        if now is not None:
            finite(now)
        if not isinstance(browse, str) or not browse or not isinstance(note, dict):
            raise ValueError('Album note output requires a release ID and an object')
        if expected_request is not None and not self.repository.matches(browse, expected_request):
            return False
        reference = self.files.publish(recommendation_outputs.encode('album_notes', browse, note))
        with self.repository.transaction():
            if expected_request is not None and not self.repository.matches(browse, expected_request):
                return False
            self.repository.replace_output(browse, reference, now, self.NOTE_LIMIT)
            self.repository.remove_request(browse, expected_request)
        return True

    def next_request(self):
        if self.installed:
            return self.repository.next_request()
        queued = self.proposals.get_proposals('album_note_requests') or {}
        return (next(iter(queued.values())), None) if queued else (None, None)

    def wake(self, now):
        notes = self.proposals.get_proposals('album_notes') or {}
        for note in notes.values():
            if (note.get('description') or {}).get('kind') != 'generated':
                note['retry_at'] = 0
        self.proposals.put_proposals('album_notes', notes, now)
