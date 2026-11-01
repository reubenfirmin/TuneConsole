"""Paid copy belongs to an exact album and policy, independently of display output."""
import hashlib
import json

from yt_playlist.core import albums, album_description_requests as requests
from yt_playlist.core import album_description_files
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.repos.base import Repo, lookup_read, synchronized


class AlbumDescriptionsRepo(Repo):
    @lookup_read
    def summary(self):
        """Completed copy and queued work from the installed description owner."""
        if not self._store._paid_descriptions:
            notes = self._store.rec_proposals.get_proposals('album_notes') or {}
            completed = sum(bool((note.get('description') or {}).get('kind') == 'generated')
                            for note in notes.values())
            pending = bool(self._store.rec_proposals.get_proposals('album_note_requests'))
            return completed, pending
        completed = self.conn.execute(
            "SELECT COUNT(DISTINCT r.album_id) FROM album_description_requests r "
            "JOIN album_descriptions d ON d.request_id=r.id WHERE r.status='ready'").fetchone()[0]
        pending = bool(self.conn.execute(
            "SELECT 1 FROM album_description_requests WHERE status IN ('pending','claimed','sent') LIMIT 1").fetchone())
        return completed, pending

    @lookup_read
    def album_id(self, album):
        """Resolve explicit catalog references; names cannot identify a release."""
        explicit = album.get('album_id')
        browse = album.get('browse_id')
        resolved = albums.lookup(self.conn, browse) if browse else None
        if explicit is not None:
            if type(explicit) is not int or not self.conn.execute(
                    'SELECT 1 FROM albums WHERE id=?', (explicit,)).fetchone():
                return None
            explicit = requests.canonical_album(self.conn, explicit)
            if browse and resolved != explicit:
                return None
            return explicit
        return resolved

    @staticmethod
    def policy_version(configuration):
        if not isinstance(configuration, dict):
            raise ValueError('Paid description policy must be an object')
        return hashlib.sha256(canonical(['album-description-policy-1', configuration])).hexdigest()

    @lookup_read
    def cached(self, album_id, policy_version):
        request_id = requests.request_for(self.conn, album_id, policy_version)
        return requests.result(self.conn, request_id) if request_id is not None else None

    @lookup_read
    def successful_result(self, album_id):
        """Keep already-paid copy usable across policy or credential changes."""
        album_id = requests.canonical_album(self.conn, album_id)
        row = self.conn.execute('SELECT d.filename,d.content_digest FROM album_description_requests r '
            'JOIN album_descriptions d ON d.request_id=r.id WHERE r.album_id IN '
            '(SELECT ? UNION ALL SELECT retired_id FROM album_alias_reconciliations WHERE album_id=?) '
            'ORDER BY d.created_at DESC,d.id DESC LIMIT 1', (album_id, album_id)).fetchone()
        if row is None:
            return None
        return json.loads(album_description_files.read(row[0], row[1]))

    @lookup_read
    def legacy_result(self, identity):
        """Read original paid evidence without claiming it belongs to a catalog ID."""
        row = self.conn.execute('SELECT id FROM album_description_requests '
                                "WHERE legacy_identity=? AND resolution_status='legacy_unresolved'",
                                (identity,)).fetchone()
        return requests.result(self.conn, row[0]) if row else None

    @lookup_read
    def requests_today(self, now):
        return requests.usage(self.conn, now)

    @lookup_read
    def availability(self, credential, now):
        return requests.availability(self.conn, credential, now)

    @synchronized
    def claim(self, album_id, configuration, credential, daily_limit, now):
        version = self.policy_version(configuration)
        return requests.claim(self.conn, album_id, version, credential, daily_limit, now,
                              configuration=configuration)

    @synchronized
    def authorize(self, request_id, token, credential, daily_limit, now):
        return requests.authorize(self.conn, request_id, token, credential, daily_limit, now)

    @synchronized
    def finish(self, request_id, token, record, now):
        return requests.finish(self.conn, request_id, token, record, now)
