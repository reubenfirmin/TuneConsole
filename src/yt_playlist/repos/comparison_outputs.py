"""SQL heads, captured modes and file manifests for paired model output."""
from yt_playlist.core import group_evidence, model_projections
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized


class ComparisonOutputsRepo(Repo):
    def native(self):
        return self._store._model_projections

    @lookup_read
    def present(self, run, arm, format):
        return self.conn.execute('SELECT 1 FROM model_pair_heads WHERE run_id=? AND arm=? AND format=?',
                                 (run, arm, format)).fetchone() is not None

    @snapshot_read
    def document(self, run, arm, format):
        row = self.conn.execute('SELECT p.* FROM model_pair_heads h JOIN model_pair_publications p '
            'ON p.id=h.publication_id WHERE h.run_id=? AND h.arm=? AND h.format=?', (run, arm, format)).fetchone()
        if row is None:
            return None
        parts = self.conn.execute('SELECT filename,content_digest,byte_size,chunk_key FROM model_pair_files '
            'WHERE publication_id=? ORDER BY CAST(chunk_key AS INTEGER)', (row['id'],)).fetchall()
        modes = self.conn.execute('SELECT mode_id,space,centroid FROM model_pair_modes '
                                 'WHERE publication_id=? ORDER BY position', (row['id'],)).fetchall()
        return {**dict(row), 'parts': [dict(part) for part in parts], 'modes': [dict(mode) for mode in modes]}

    @synchronized
    def publish(self, prepared, durable, generation, roots):
        with self.conn.atomic(join=True):
            return model_projections.publish_files(self.conn, prepared, durable, generation=generation, roots=roots)

    @synchronized
    def publish_legacy(self, surface, raw, at, roots):
        with self.conn.atomic(join=True):
            if roots is not None:
                group_evidence.register_prepared(self.conn, *roots)
            self.conn.execute('INSERT INTO rec_proposals(surface,payload,built_at) VALUES (?,?,?) '
                'ON CONFLICT(surface) DO UPDATE SET payload=excluded.payload,built_at=excluded.built_at',
                (surface, raw, at))
