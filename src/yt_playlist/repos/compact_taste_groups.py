"""Repository interface for compact group publications and exact evidence roots."""
import json
import time
from contextlib import ExitStack

from yt_playlist.core import group_evidence, group_storage
from yt_playlist.core.db_revisions import column_group_reads, keyed_reads
from .base import lookup_read, snapshot_read, synchronized
from .group_preparation import GroupPreparationRepo
from . import compact_group_publication as publication, group_publication as batches, group_queries


class CompactTasteGroupsRepo(GroupPreparationRepo):
    staged_tables = publication.STAGED_TABLES

    def conflicting_build(self, prepared):
        return publication.conflicting_build(self, prepared)

    @lookup_read
    def head_generation(self):
        return group_queries.head_generation(self.conn)

    @lookup_read
    def has_adaptive_head(self):
        return self.conn.execute(
            'SELECT 1 FROM group_generations h JOIN group_versions v ON v.generation_id=h.id '
            'WHERE h.is_active=1 AND v.legacy=0 AND v.position IS NOT NULL LIMIT 1'
        ).fetchone() is not None

    @lookup_read
    def head_id(self):
        with column_group_reads(self._store, 'group_generations', 'selection'):
            row = self.conn.execute('SELECT id FROM group_generations WHERE is_active=1').fetchone()
        return row[0] if row else None

    @snapshot_read
    def head_checkpoint(self):
        return group_queries.head_checkpoint(self.conn)

    @snapshot_read
    def head(self, *, members=False):
        identity = self.head_id()
        if identity is not None:
            with keyed_reads(self._store, 'group_generations', (identity,)):
                return group_queries.head(self.conn, members=members, generation_id=identity)
        return None

    @snapshot_read
    def preview_head(self):
        """Guard the served hierarchy without consuming its preparation manifest.

        Hidden builds and historical cleanup touch these owners too. Every row
        consumed below belongs to the captured head or its exact content IDs;
        changing that head or any of those rows still invalidates the preview.
        """
        head = self.head_id()
        if head is None:
            return None
        with ExitStack() as scopes:
            for table in ('group_generations', 'group_versions'):
                scopes.enter_context(keyed_reads(self._store, table, (head,)))
            selected = self.conn.execute('SELECT group_id,id FROM group_versions '
                                         'WHERE generation_id=?', (head,)).fetchall()
            scopes.enter_context(keyed_reads(self._store, 'taste_groups', [row[0] for row in selected]))
            generation = self.conn.execute('SELECT policy_version FROM group_generations WHERE id=?',
                                            (head,)).fetchone()
            scopes.enter_context(keyed_reads(self._store, 'recommendation_policies', (generation[0],)))
            return group_queries.head(self.conn, generation_id=head)

    @snapshot_read
    def definitions(self, generation, *, members=False):
        return group_queries.definitions(self.conn, generation, members=members)

    @snapshot_read
    def definition(self, version, *, generation=None):
        return group_queries.definition(self.conn, version, generation=generation)

    @snapshot_read
    def correction_definition(self, version, *, generation=None):
        return group_queries.correction_definition(self.conn, version, generation=generation)

    @snapshot_read
    def learning_head(self):
        return group_queries.learning_head(self.conn)

    @snapshot_read
    def leaf_members(self, generation, *, keys=None):
        return group_queries.leaf_members(self.conn, generation, keys=keys)

    @snapshot_read
    def recording_groups(self, generation):
        """Read the membership index for one published musical hierarchy."""
        rows = self.conn.execute('SELECT m.recording_id,v.external_version '
            'FROM group_generations g '
            'CROSS JOIN group_versions v ON v.generation_id=g.id '
            'CROSS JOIN group_members m ON m.group_version_id=v.id '
            'WHERE g.external_key=? AND g.is_published=1 AND v.position IS NOT NULL '
            'AND m.recording_id IS NOT NULL', (generation,))
        rows.row_factory = None
        memberships = {}
        for recording, version in rows.fetchall():
            memberships.setdefault(recording, []).append(version)
        return memberships

    @snapshot_read
    def legacy_versions(self):
        return group_queries.legacy_versions(self.conn)

    def cleanup_builds(self, *, generation=None):
        return publication.cleanup(self, generation=generation)

    def abandon_staged(self, prepared):
        return publication.abandon(self, prepared)

    @synchronized
    def assert_idle(self):
        if self.conn.in_transaction:
            raise RuntimeError('Group publication requires an idle serving connection')

    @snapshot_read
    def recording_ids(self, keys):
        result = {}
        for start in range(0, len(keys), 128):
            batch = keys[start:start + 128]
            result.update(self.conn.execute('SELECT identity_key,id FROM recordings WHERE identity_key IN ('
                                           + ','.join('?' for _ in batch) + ')', batch))
        return result

    @lookup_read
    def generation_reference(self, prepared, policy_version):
        return publication.generation_reference(self.conn, prepared, policy_version)

    def claim_generation(self, prepared, policy_version, metadata_reference):
        return publication.claim(self, prepared, policy_version, metadata_reference)

    def install_identities(self, prepared, generation_id, owner):
        return publication.identities(self, prepared, generation_id, owner)

    def stages(self, generation_id, owner):
        return publication.stages(self, generation_id, owner)

    def install_definitions(self, prepared, generation_id, owner, plans, files):
        return publication.install_definitions(self, prepared, generation_id, owner, plans, files)

    def install_members(self, generation_id, owner, version_id, rows, identities, inputs):
        with batches._write(self) as conn:
            publication._touch(conn, generation_id, owner)
            group_storage.insert_members(conn, version_id, rows, identities.get, inputs)

    @snapshot_read
    def version_facts(self, version_id):
        facts = group_storage.version_facts(self.conn, version_id)
        facts['policy'] = group_queries._policy(self.conn, facts['version']['policy_version'])
        return facts

    def seal(self, generation_id, owner, version_id):
        return publication.seal(self, generation_id, owner, version_id)

    def mark_ready(self, generation_id, owner, count):
        return publication.mark_ready(self, generation_id, owner, count)

    @lookup_read
    def expired_builds(self):
        return self.conn.execute('SELECT 1 FROM group_generations WHERE build_expires_at<? LIMIT 1',
                                 (time.time(),)).fetchone() is not None

    @snapshot_read
    def resumable_build(self, fingerprint, serial, policy_version, parent):
        row = self.conn.execute('SELECT g.external_key generation,g.created_at FROM group_generations g '
            'LEFT JOIN group_generations p ON p.id=g.build_parent_id '
            'WHERE g.input_fingerprint=? AND g.input_serial=? AND g.policy_version=? AND p.external_key IS ? '
            "AND g.build_owner<>'cleanup' AND g.build_output_digest IS NOT NULL ORDER BY g.created_at LIMIT 1",
            (fingerprint, serial, policy_version, parent)).fetchone()
        return dict(row) if row else None

    @lookup_read
    def staged_at(self, generation):
        row = self.conn.execute('SELECT created_at FROM group_generations '
                                'WHERE external_key=? AND build_owner IS NOT NULL', (generation,)).fetchone()
        return row[0] if row else None

    @synchronized
    def finish_staged(self, generation, owner):
        with self.conn.atomic(join=True):
            return publication.finish(self.conn, generation, owner)

    @synchronized
    def retain_evidence(self, owner, *, strict=True):
        with self.conn.atomic(join=True):
            return group_evidence.retain_owner(self.conn, owner, strict=strict)

    @synchronized
    def release_evidence(self, kind, identity):
        with self.conn.atomic(join=True):
            return group_evidence.release(self.conn, kind, identity)
