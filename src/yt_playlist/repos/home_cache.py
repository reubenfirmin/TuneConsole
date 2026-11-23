"""SQL metadata and atomic heads for the Home mix cache."""
from yt_playlist.core import home_outputs, publication_files
from yt_playlist.repos.base import Repo, lookup_read, synchronized
from . import recommendation_file_writes


class HomeCacheRepo(Repo):
    @synchronized
    def input_revision(self, dependencies):
        revisions = self._store._snapshot_revisions
        external = self.conn.execute('PRAGMA data_version').fetchone()[0]
        return revisions, external, revisions.capture(dependencies)

    @synchronized
    def reserve_file(self, surface, chunk_key, integrity):
        with self.conn.atomic():
            return recommendation_file_writes.reserve(self.conn, 'home_display_files', surface, chunk_key, integrity)

    @lookup_read
    def unchanged(self, prepared):
        """Identical bytes retain their existing head, files and evidence roots."""
        row = publication_files.fields(self.conn, home_outputs.PREFIX + prepared.surface, 'content_digest')
        return bool(row and row[0] == prepared.digest)

    @lookup_read
    def published_after(self, surface, started_at):
        row = publication_files.fields(self.conn, home_outputs.PREFIX + surface, 'built_at')
        return bool(row and row[0] is not None and row[0] > started_at)

    @synchronized
    def document(self, surface):
        return home_outputs.manifest(self.conn, surface)

    @synchronized
    def publication_id(self, surface):
        row = publication_files.fields(self.conn, home_outputs.PREFIX + surface, 'id')
        return row[0] if row else None

    @synchronized
    def publish(self, prepared, filenames, policies, roots, *, input_revision=None, file_ids=()):
        with self.conn.atomic(join=True):
            if self.unchanged(prepared) and not file_ids:
                return
            if not file_ids or len(file_ids) != len(filenames):
                raise ValueError('Home files require their pending SQL identities')
            for identity, (surface, key, *reference) in zip(file_ids, filenames):
                recommendation_file_writes.complete(self.conn, identity, 'home_display_files', surface, key, reference)
            return home_outputs.publish_files(self.conn, prepared, filenames, policies=policies, roots=roots,
                                              input_revision=input_revision)

    @lookup_read
    def present(self, surface):
        return home_outputs.present(self.conn, surface)
