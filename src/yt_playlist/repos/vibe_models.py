"""Serialized runtime access to shared vibe model SQL records."""
from yt_playlist.core.vibe_model_queries import VibeModelQueries
from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos import vibe_model_file_writes


class VibeModelsRepo(Repo):
    @synchronized
    def reserve_file(self, generation, size):
        with self.conn.atomic():
            return vibe_model_file_writes.reserve(self.conn, generation, size)

    @synchronized
    def complete_file(self, identity, generation, size):
        with self.conn.atomic():
            vibe_model_file_writes.complete(self.conn, identity, generation, size)

    @synchronized
    def file_reference(self, generation):
        with keyed_reads(self._store, 'collection_file_writes', (bytes.fromhex(generation).hex(),)):
            return VibeModelQueries.file_reference(self, generation)

    @synchronized
    def assert_idle(self):
        return VibeModelQueries.assert_idle(self)

    @synchronized
    def measurements(self, identities):
        return VibeModelQueries.measurements(self, identities)

    @synchronized
    def receipt_facts(self, prediction, artists):
        return VibeModelQueries.receipt_facts(self, prediction, artists)

    @synchronized
    def install_receipts(self, generations, roots, markers):
        return VibeModelQueries.install_receipts(self, generations, roots, markers)

    @synchronized
    def referenced(self, generation):
        return VibeModelQueries.referenced(self, generation)
