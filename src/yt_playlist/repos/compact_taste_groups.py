"""Repository interface for compact group publications and exact evidence roots."""
from contextlib import contextmanager
import time

from yt_playlist.core import group_documents, group_evidence, group_storage, policy_documents
from .base import lookup_read, snapshot_read, synchronized
from .taste_groups import TasteGroupsRepo
from . import compact_group_publication as publication, group_queries, group_retention


class CompactTasteGroupsRepo(TasteGroupsRepo):
    staged_tables = publication.STAGED_TABLES

    @contextmanager
    def _read(self):
        # Serving calls normally use Store's already pinned private reader.
        # An enclosing writer may read its uncommitted imports too; the file
        # lock still precedes the first group query in either case.
        with policy_documents.retained_reader(group_documents.directory(self.conn)):
            owned = not self.conn.in_transaction
            if owned:
                self.conn.execute('BEGIN')
            try:
                yield self.conn
            finally:
                if owned:
                    self.conn.rollback()

    @lookup_read
    def head_generation(self):
        return group_queries.head_generation(self.conn)

    @snapshot_read
    def head_checkpoint(self):
        with self._read() as conn:
            return group_queries.head_checkpoint(conn)

    @snapshot_read
    def head(self, *, members=False):
        with self._read() as conn:
            return group_queries.head(conn, members=members)

    @snapshot_read
    def definitions(self, generation, *, members=False):
        with self._read() as conn:
            return group_queries.definitions(conn, generation, members=members)

    @snapshot_read
    def definition(self, version, *, generation=None):
        with self._read() as conn:
            return group_queries.definition(conn, version, generation=generation)

    @snapshot_read
    def correction_definition(self, version, *, generation=None):
        return group_queries.correction_definition(self.conn, version, generation=generation)

    @snapshot_read
    def learning_head(self):
        with self._read() as conn:
            return group_queries.learning_head(conn)

    @snapshot_read
    def leaf_members(self, generation):
        with self._read() as conn:
            return group_queries.leaf_members(conn, generation)

    @snapshot_read
    def lineage(self):
        with self._read() as conn:
            return group_queries.lineage(conn)

    @snapshot_read
    def successors(self, version, *, limit=512):
        with self._read() as conn:
            return group_queries.successors(conn, version, limit=limit)

    @snapshot_read
    def legacy_versions(self):
        return group_queries.legacy_versions(self.conn)

    def stage(self, prepared):
        return publication.stage(self, prepared)

    def cleanup_staged(self):
        return publication.cleanup(self) or group_retention.step(self)

    def abandon_staged(self, prepared):
        return publication.abandon(self, prepared)

    @lookup_read
    def expired_builds(self):
        return self.conn.execute('SELECT 1 FROM group_builds WHERE expires_at<? LIMIT 1', (time.time(),)).fetchone() is not None

    @snapshot_read
    def resumable_build(self, fingerprint, serial, policy, parent):
        row = self.conn.execute('SELECT g.external_key generation,g.created_at FROM group_builds b '
            'JOIN group_generations g ON g.id=b.generation_id LEFT JOIN group_generations p ON p.id=b.parent_generation_id '
            'WHERE g.input_fingerprint=? AND g.input_serial=? AND g.policy_version=? AND p.external_key IS ? '
            "AND b.owner<>'cleanup' AND b.output_digest IS NOT NULL ORDER BY g.created_at LIMIT 1",
            (fingerprint, serial, group_storage.policy_version(policy), parent)).fetchone()
        return dict(row) if row else None

    @lookup_read
    def staged_at(self, generation):
        row = self.conn.execute('SELECT g.created_at FROM group_generations g JOIN group_builds b '
                               'ON b.generation_id=g.id WHERE g.external_key=?', (generation,)).fetchone()
        return row[0] if row else None

    @synchronized
    def finish_staged(self, generation, owner):
        return publication.finish(self.conn, generation, owner)

    @synchronized
    def publish(self, prepared):
        return publication.publish(self, prepared)

    @synchronized
    def retain_evidence(self, kind, identity, context, created_at, *, strict=True):
        return group_evidence.retain(self.conn, kind, identity, context, created_at, strict=strict)

    @synchronized
    def release_evidence(self, kind, identity):
        return group_evidence.release(self.conn, kind, identity)
