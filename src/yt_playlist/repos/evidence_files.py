"""Listening evidence receipts, independent of captured JSON and file access."""
from yt_playlist.core import evidence_maintenance, playback_bindings
from yt_playlist.repos.base import Repo, lookup_read, synchronized
from yt_playlist.repos import evidence_file_writes as writes


class EvidenceFileRepo(Repo):
    @lookup_read
    def references(self, keys=None):
        return writes.references(self.conn, keys)

    @synchronized
    def reference_for_file(self, identity):
        return writes.reference_for_file(self.conn, identity)

    @synchronized
    def reserve(self, records):
        with self.conn.atomic():
            return writes.reserve(self.conn, records)

    @synchronized
    def complete(self, records, previous, *, playback=None):
        with self.conn.atomic():
            writes.complete(self.conn, records, previous)
            if playback is not None:
                playback_bindings.attach(self.conn, records['response', playback['id']][0], playback)

    @synchronized
    def retire_files(self, identities):
        with self.conn.atomic():
            for identity in identities:
                self.conn.execute("UPDATE collection_file_writes SET selected=0 WHERE kind='listening-evidence' "
                                  'AND file_address=?', (identity,))

    @lookup_read
    def maintenance_state(self):
        return evidence_maintenance.state(self.conn)

    @synchronized
    def update_maintenance(self, *, pending=None, phase=None, checkpoint_after=None):
        with self.conn.atomic():
            fields = {'pending': None if pending is None else int(pending),
                      'phase': phase, 'checkpoint_after': checkpoint_after}
            evidence_maintenance.update(self.conn, {key: value for key, value in fields.items() if value is not None})
