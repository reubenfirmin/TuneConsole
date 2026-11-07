"""SQL report manifests, serving metadata, and publication heads."""
from yt_playlist.core import recommendation_reports as reports
from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized


class RecommendationReportsRepo(Repo):
    @lookup_read
    def native(self):
        return self._store._recommendation_reports

    def _owned(self):
        return self._store._recommendation_reports or reports.prepared_owner(self.conn)

    @snapshot_read
    def document(self, surface):
        with keyed_reads(self._store, 'rec_proposals', (surface,)):
            if self._owned():
                return None, reports.manifest(self.conn, surface)
            row = self.conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
            return row[0] if row else None, None

    @lookup_read
    def present(self, surface):
        with keyed_reads(self._store, 'rec_proposals', (surface,)):
            if self._owned():
                return reports.present(self.conn, surface)
            return self.conn.execute("SELECT 1 FROM rec_proposals WHERE surface=? AND payload NOT IN ('null','{}')",
                                     (surface,)).fetchone() is not None

    @lookup_read
    def checkpoint(self, surface):
        with keyed_reads(self._store, 'rec_proposals', (surface,)):
            owned = self._owned()
            return owned, reports.checkpoint(self.conn, surface) if owned else None

    @snapshot_read
    def usage(self):
        with keyed_reads(self._store, 'rec_proposals', ('listening_choices',)):
            owned = self._owned()
            return owned, reports.choice_usage(self.conn) if owned else None

    @synchronized
    def publish(self, prepared, durable):
        with self.conn.atomic(join=True):
            return reports.register(self.conn, prepared, durable)
