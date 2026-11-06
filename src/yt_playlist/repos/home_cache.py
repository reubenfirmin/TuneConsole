"""SQL metadata and atomic heads for the Home mix cache."""
from yt_playlist.core import home_outputs, prepared_references
from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.repos.base import Repo, snapshot_read, synchronized


class HomeCacheRepo(Repo):
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
    def publish(self, prepared, filenames, policies, contexts, recommendation_generations, *, input_revision=None):
        if self._store._home_display_files:
            return home_outputs.publish_files(self.conn, prepared, filenames, store=self._store,
                policies=policies, resolved_contexts=contexts, recommendation_generations=recommendation_generations,
                input_revision=input_revision)
        prepared_references.replace_prepared(self._store, prepared.surface, prepared.value, prepared.built_at,
            resolved_contexts=contexts, recommendation_generations=recommendation_generations)
        self.conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
            'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
            (prepared.surface, prepared.raw, prepared.built_at))
