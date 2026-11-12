"""SQL metadata and atomic heads for the Home mix cache."""
from yt_playlist.core import group_evidence, home_outputs
from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized


class HomeCacheRepo(Repo):
    @lookup_read
    def unchanged(self, prepared):
        """Identical bytes retain their existing head, files and evidence roots."""
        if self._store._home_display_files:
            row = self.conn.execute('SELECT p.content_digest FROM home_display_heads h '
                'JOIN home_display_publications p ON p.id=h.publication_id WHERE h.surface=?',
                (prepared.surface,)).fetchone()
            return bool(row and row[0] == prepared.digest)
        row = self.conn.execute('SELECT payload FROM rec_proposals WHERE surface=?',
                                (prepared.surface,)).fetchone()
        return bool(row and row[0] == prepared.raw)

    @lookup_read
    def native(self):
        return self._store._home_display_files

    @snapshot_read
    def document(self, surface):
        with keyed_reads(self._store, 'rec_proposals', (surface,)):
            if self._store._home_display_files:
                return None, home_outputs.manifest(self.conn, surface)
            from yt_playlist.core.preparation import home_publication, home_outputs as preparation
            handled, header = home_publication.header(self.conn, surface)
            if handled:
                if header is None:
                    return None, None
                parts = self.conn.execute(f'SELECT CAST(position AS TEXT),filename,digest,byte_size FROM {preparation.PARTS} '
                    'WHERE surface=? AND generation=? ORDER BY position', (surface, header['generation'])).fetchall()
                if len(parts) != header['part_count']:
                    raise ValueError('Incomplete Home display parts')
                return None, (header['raw_digest'], header['raw_size'], parts)
            row = self.conn.execute('SELECT payload FROM rec_proposals WHERE surface=?', (surface,)).fetchone()
            return row[0] if row else None, None

    @synchronized
    def publish(self, prepared, filenames, policies, roots, *, input_revision=None):
        with self.conn.atomic(join=True):
            if self.unchanged(prepared):
                return
            if self._store._home_display_files:
                return home_outputs.publish_files(self.conn, prepared, filenames, policies=policies, roots=roots,
                                                  input_revision=input_revision)
            self._publish_legacy(prepared.surface, prepared.raw, prepared.built_at, roots)

    def _publish_legacy(self, surface, raw, now, roots):
        if roots is not None:
            group_evidence.register_prepared(self.conn, *roots)
        self.conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
            'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
            (surface, raw, now))

    @synchronized
    def publish_legacy(self, surface, raw, now, roots):
        with self.conn.atomic(join=True):
            self._publish_legacy(surface, raw, now, roots)

    @lookup_read
    def present(self, surface):
        with keyed_reads(self._store, 'rec_proposals', (surface,)):
            if self._store._home_display_files:
                return home_outputs.present(self.conn, surface)
            from yt_playlist.core.preparation import home_publication
            handled, header = home_publication.header(self.conn, surface)
            if handled:
                return header is not None and bool(header['has_content'])
            return self.conn.execute("SELECT 1 FROM rec_proposals WHERE surface=? AND payload NOT IN ('null','{}')",
                                     (surface,)).fetchone() is not None
