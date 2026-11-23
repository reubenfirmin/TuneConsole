"""SQL report manifests, serving metadata, and publication heads."""
from yt_playlist.core import recommendation_reports as reports
from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized
from . import recommendation_file_writes


class RecommendationReportsRepo(Repo):
    @synchronized
    def reserve_file(self, surface, chunk_key, integrity):
        with self.conn.atomic():
            return recommendation_file_writes.reserve(self.conn, 'recommendation_report_files', surface, chunk_key, integrity)

    @snapshot_read
    def manifest(self, surface):
        with keyed_reads(self._store, 'rec_proposals', (surface,)):
            return reports.manifest(self.conn, surface)

    @lookup_read
    def present(self, surface):
        with keyed_reads(self._store, 'rec_proposals', (surface,)):
            return reports.present(self.conn, surface)

    @lookup_read
    def checkpoint(self, surface):
        with keyed_reads(self._store, 'rec_proposals', (surface,)):
            return reports.checkpoint(self.conn, surface)

    @snapshot_read
    def usage(self):
        with keyed_reads(self._store, 'rec_proposals', ('listening_choices',)):
            return reports.choice_usage(self.conn)

    @synchronized
    def publish(self, prepared, durable, *, file_ids):
        with self.conn.atomic(join=True):
            if len(file_ids) != len(durable):
                raise ValueError('Report files require their pending SQL identities')
            for identity, (surface, key, *reference) in zip(file_ids, durable):
                recommendation_file_writes.complete(self.conn, identity, 'recommendation_report_files', surface, key, reference)
            return reports.register(self.conn, prepared, durable)
