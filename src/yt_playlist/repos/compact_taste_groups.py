"""Repository interface for compact group publications and exact evidence roots."""
import time

from yt_playlist.core import group_evidence, group_storage
from .base import lookup_read, snapshot_read, synchronized
from .group_preparation import GroupPreparationRepo
from . import compact_group_publication as publication, group_publication as batches, group_queries


class CompactTasteGroupsRepo(GroupPreparationRepo):
    staged_tables = publication.STAGED_TABLES

    @lookup_read
    def head_generation(self):
        return group_queries.head_generation(self.conn)

    @snapshot_read
    def head_checkpoint(self):
        return group_queries.head_checkpoint(self.conn)

    @snapshot_read
    def head(self, *, members=False):
        return group_queries.head(self.conn, members=members)

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
    def leaf_members(self, generation):
        return group_queries.leaf_members(self.conn, generation)

    @snapshot_read
    def lineage(self):
        return group_queries.lineage(self.conn)

    @snapshot_read
    def successors(self, version, *, limit=512):
        return group_queries.successors(self.conn, version, limit=limit)

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

    def clear_build_conflicts(self, prepared, plans):
        return publication._clear_foreign_conflicts(self, prepared, plans)

    def claim_generation(self, prepared, inputs, policy_version, policy_reference, metadata_reference):
        return publication._claim(self, prepared, inputs, policy_version, policy_reference, metadata_reference)

    def install_inputs(self, generation_id, manifest_id, owner, rows):
        return publication._inputs(self, generation_id, manifest_id, owner, rows)

    def install_identities(self, prepared, generation_id, owner):
        return publication._identities(self, prepared, generation_id, owner)

    @snapshot_read
    def selected_groups(self, generation_id, owner):
        if self.conn.execute('SELECT 1 FROM group_builds WHERE generation_id=? AND owner=? AND ready=0',
                             (generation_id, owner)).fetchone() is None:
            raise RuntimeError('Musical group build is no longer owned by this worker')
        return dict(self.conn.execute('SELECT t.external_key,x.external_version FROM generation_groups x '
            'JOIN taste_groups t ON t.id=x.group_id WHERE x.generation_id=?', (generation_id,)))

    def definition_candidates(self, generation_id, owner, batch):
        return publication.definition_candidates(self, generation_id, owner, batch)

    def install_definitions(self, prepared, generation_id, owner, rows):
        return publication.install_definitions(self, prepared, generation_id, owner, rows)

    def group_state(self, generation_id, owner, plan):
        return publication.group_state(self, generation_id, owner, plan)

    def install_members(self, generation_id, owner, version_id, rows, identities, inputs):
        with batches._write(self) as conn:
            publication._touch(conn, generation_id, owner)
            group_storage.insert_members(conn, version_id, rows, identities.get, inputs)

    def install_representatives(self, generation_id, owner, version_id, rows, identities):
        with batches._write(self) as conn:
            publication._touch(conn, generation_id, owner)
            group_storage.insert_representatives(conn, version_id, rows, identities.get)

    @snapshot_read
    def version_facts(self, version_id):
        facts = group_storage.version_facts(self.conn, version_id)
        facts['policy'] = group_queries._policy(self.conn, facts['version']['policy_version'])
        return facts

    def link_group(self, generation_id, owner, group_id, version_id, position, alias):
        return publication.link_group(self, generation_id, owner, group_id, version_id, position, alias)

    @snapshot_read
    def lineage_prefix(self, generation_id):
        return publication._count(self.conn, generation_id, ('group_generation_lineage',))

    @snapshot_read
    def lineage_facts(self, pairs):
        result = {}
        for source, target in pairs:
            row = self.conn.execute('SELECT * FROM group_lineage WHERE source_alias=? AND target_alias=?',
                                    (source, target)).fetchone()
            if row is not None:
                result[source, target] = dict(row)
        return result

    def install_lineage(self, generation_id, owner, rows):
        with batches._write(self) as conn:
            publication._touch(conn, generation_id, owner)
            publication.insert_lineage(conn, generation_id, rows)

    def install_caches(self, prepared, generation_id, owner, identities):
        return publication._caches(self, prepared, generation_id, owner, identities.get)

    def mark_ready(self, generation_id, owner, count):
        return publication.mark_ready(self, generation_id, owner, count)

    @lookup_read
    def expired_builds(self):
        return self.conn.execute('SELECT 1 FROM group_builds WHERE expires_at<? LIMIT 1', (time.time(),)).fetchone() is not None

    @snapshot_read
    def resumable_build(self, fingerprint, serial, policy_version, parent):
        row = self.conn.execute('SELECT g.external_key generation,g.created_at FROM group_builds b '
            'JOIN group_generations g ON g.id=b.generation_id LEFT JOIN group_generations p ON p.id=b.parent_generation_id '
            'WHERE g.input_fingerprint=? AND g.input_serial=? AND g.policy_version=? AND p.external_key IS ? '
            "AND b.owner<>'cleanup' AND b.output_digest IS NOT NULL ORDER BY g.created_at LIMIT 1",
            (fingerprint, serial, policy_version, parent)).fetchone()
        return dict(row) if row else None

    @lookup_read
    def staged_at(self, generation):
        row = self.conn.execute('SELECT g.created_at FROM group_generations g JOIN group_builds b '
                               'ON b.generation_id=g.id WHERE g.external_key=?', (generation,)).fetchone()
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
