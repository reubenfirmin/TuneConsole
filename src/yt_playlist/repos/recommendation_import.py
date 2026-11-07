"""SQL operations for importing a prepared legacy recommendation generation."""
from .bundle_storage import BundleInputs
from . import recommendation_publication as publication
from yt_playlist.core import recommendation_pools


class RecommendationImportRepo(BundleInputs):
    def in_transaction(self):
        with self._lock:
            return self.conn.in_transaction

    def file_location(self):
        with self._lock:
            return self.conn.execute('PRAGMA database_list').fetchone()[2], getattr(
                self.conn, '_policy_memory_key', str(id(self.conn)))

    def generation_exists(self, key):
        with self._lock:
            return bool(self.conn.execute('SELECT 1 FROM recommendation_generations WHERE external_key=?', (key,)).fetchone())

    def published_group(self, generation):
        with self._lock:
            return bool(self.conn.execute('SELECT 1 FROM group_publications WHERE generation_id=?', (generation,)).fetchone())

    def group_contains(self, generation, version):
        with self._lock:
            return bool(self.conn.execute('SELECT 1 FROM generation_groups WHERE generation_id=? AND group_version_id=?',
                                          (generation, version)).fetchone())

    def pool_owner(self, digest):
        with self._lock:
            row = self.conn.execute('SELECT p.id,p.complete,EXISTS(SELECT 1 FROM recommendation_pool_retirements r '
                'WHERE r.pool_id=p.id) AS retiring FROM recommendation_pools p WHERE p.content_digest=?', (digest,)).fetchone()
            return {'id': row[0], 'complete': row[1], 'retiring': row[2]} if row else None

    def known_recordings(self, identities):
        with self._lock:
            if not identities:
                return set()
            if len(identities) > publication.BATCH_ROWS:
                raise ValueError('Recommendation recording lookup exceeds its batch budget')
            marks = ','.join('?' for _ in identities)
            return {row[0] for row in self.conn.execute(f'SELECT id FROM recordings WHERE id IN ({marks})', identities)}

    def create(self, plan, digest):
        with self._lock:
            return publication._create(self.conn, plan, digest)

    def create_pool(self, prepared):
        with self._lock:
            return publication._create_pool(self.conn, prepared)

    def append_pool(self, identity, candidates, start):
        with self._lock:
            return recommendation_pools.append(self.conn, identity, candidates, start)

    def seal_pool(self, identity):
        with self._lock:
            self.conn.execute('UPDATE recommendation_pools SET complete=1 WHERE id=?', (identity,))

    def attach_pool(self, generation, slot, identity):
        with self._lock:
            self.conn.execute('INSERT INTO recommendation_surface_pools VALUES (?,?,?,?,?)',
                (generation, slot.surface, slot.mode_key, slot.purpose, identity))

    def record_output(self, plan, generation, output, file):
        with self._lock:
            publication._output(self.conn, plan, generation, output, file)

    def ready(self, plan, generation):
        with self._lock:
            publication._ready(self.conn, plan, generation)

    def finish(self, build, at):
        with self._lock:
            return publication.finish(self.conn, build, at)
