"""Listening evidence receipts, independent of captured JSON and file access."""
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos import evidence_file_writes as writes


class EvidenceFileRepo(Repo):
    @synchronized
    def references(self, keys=None):
        return writes.references(self.conn, keys)

    @synchronized
    def reserve(self, records):
        with self.conn.atomic():
            return writes.reserve(self.conn, records)

    @synchronized
    def complete(self, records, previous):
        with self.conn.atomic():
            writes.complete(self.conn, records, previous)

    @synchronized
    def retire_files(self, identities):
        with self.conn.atomic():
            for identity in identities:
                self.conn.execute('DELETE FROM evidence_file_references WHERE file_id=?', (identity,))

    @synchronized
    def begin_maintenance(self):
        with self.conn.atomic():
            writes.install(self.conn)
            self.conn.execute("INSERT INTO evidence_file_maintenance(id,file_status) VALUES (1,'pending') "
                              "ON CONFLICT(id) DO UPDATE SET file_status='pending'")
        return 1

    @synchronized
    def finish_maintenance(self, identity):
        with self.conn.atomic():
            self.conn.execute("UPDATE evidence_file_maintenance SET file_status='ready' WHERE id=?", (identity,))
