"""SQL operations for importing a prepared legacy recommendation generation."""
from .bundle_storage import BundleInputs
from . import recommendation_file_writes, recommendation_publication as publication
from yt_playlist.core import recommendation_chunks, recommendation_pools


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
            return bool(self.conn.execute('SELECT 1 FROM group_generations WHERE id=? AND is_published=1',
                                          (generation,)).fetchone())

    def pool_owner(self, digest):
        with self._lock:
            row = self.conn.execute('SELECT id,complete,retiring FROM recommendation_pools WHERE content_digest=?',
                                    (digest,)).fetchone()
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
            self.conn.execute('INSERT INTO recommendation_surface_pools (generation_id,surface,mode_key,purpose,pool_id) '
                              'VALUES (?,?,?,?,?)',
                (generation, slot.surface, slot.mode_key, slot.purpose, identity))

    def reserve_output(self, output, integrity):
        with self._lock:
            return recommendation_file_writes.reserve(
                self.conn, recommendation_chunks.OWNER, output.surface, output.chunk_key, integrity)

    def record_output(self, plan, generation, output, reference, identity):
        with self._lock:
            recommendation_file_writes.complete(
                self.conn, identity, recommendation_chunks.OWNER, output.surface, output.chunk_key, reference)
            publication._output(self.conn, plan, generation, output, reference[0], identity)

    def ready(self, plan, generation):
        with self._lock:
            publication._ready(self.conn, plan, generation)

    def finish(self, build, at):
        with self._lock:
            return publication.finish(self.conn, build, at)
