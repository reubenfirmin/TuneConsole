"""Bounded SQL facts and mutations for an explicit group-retention policy."""
from yt_playlist.core import group_checkpoint_retention, group_retention_progress
from .base import Repo, snapshot_read, synchronized
from . import group_retention, group_retention_walk

ROOTS = {
    'head': ('group_head', 'generation_id'),
    'evidence': ('group_evidence_generations', 'generation_id'),
    'build': ('group_builds', 'generation_id'),
    'build_parent': ('group_builds', 'parent_generation_id'),
    'recommendation': ('recommendation_generations', 'group_generation_id'),
    'checkpoint': ('group_preparation_state', 'generation_id'),
}
ROOT_INDEXES = {'evidence': 'ix_group_evidence_generation', 'build_parent': 'ix_compact_group_build_parent'}


class GroupRetentionRepo(Repo):
    @snapshot_read
    def retention_state(self):
        row = self.conn.execute('SELECT roots_complete FROM group_retention_state WHERE id=1').fetchone()
        walking = group_retention_progress.available(self.conn)
        return {'complete': bool(row and row[0]), 'walk': walking,
                'revision': self.conn.execute('SELECT revision FROM group_retention_epoch WHERE id=1').fetchone()[0]
                            if walking else None,
                'checkpoint': group_checkpoint_retention.available(self.conn),
                'recommendations': self.conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' "
                    "AND name='recommendation_generations'").fetchone() is not None}

    @snapshot_read
    def root_references(self, roots, limit):
        selections = [f'SELECT {ROOTS[name][1]} FROM {ROOTS[name][0]} WHERE {ROOTS[name][1]} IS NOT NULL'
                      for name, _expand in roots]
        generations = [row[0] for row in self.conn.execute(' UNION '.join(selections) + ' LIMIT ?', (limit,))]
        selections = ['SELECT external_version FROM group_evidence_versions']
        selections.extend(f'SELECT x.external_version FROM generation_groups x JOIN {ROOTS[name][0]} r '
                          f'ON r.{ROOTS[name][1]}=x.generation_id'
                          for name, expand in roots if expand)
        aliases = [row[0] for row in self.conn.execute(' UNION '.join(selections) + ' LIMIT ?', (limit,))]
        return generations, aliases

    @snapshot_read
    def published_successors(self, source, limit):
        return [row[0] for row in self.conn.execute('SELECT l.target_alias FROM group_lineage l WHERE l.source_alias=? '
            'AND EXISTS(SELECT 1 FROM group_generation_lineage x JOIN group_publications p '
            'ON p.generation_id=x.generation_id WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias) '
            'LIMIT ?', (source, limit))]

    @snapshot_read
    def alias_origins(self, aliases):
        result = set()
        for alias in aliases:
            row = self.conn.execute('SELECT first_generation_id FROM group_version_aliases WHERE external_version=?',
                                    (alias,)).fetchone()
            if row is not None:
                result.add(row[0])
        return result

    @snapshot_read
    def lineage_origins(self, edges):
        result = set()
        for source, target in edges:
            row = self.conn.execute('SELECT x.generation_id FROM group_generation_lineage x JOIN group_publications p '
                'ON p.generation_id=x.generation_id WHERE x.source_alias=? AND x.target_alias=? '
                'ORDER BY x.generation_id LIMIT 1', (source, target)).fetchone()
            if row is not None:
                result.add(row[0])
        return result

    @snapshot_read
    def published_edges(self, limit):
        return list(map(tuple, self.conn.execute('SELECT l.source_alias,l.target_alias FROM group_lineage l WHERE EXISTS('
            'SELECT 1 FROM group_generation_lineage x JOIN group_publications p ON p.generation_id=x.generation_id '
            'WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias) AND NOT EXISTS('
            'SELECT 1 FROM group_generation_lineage x JOIN group_builds b ON b.generation_id=x.generation_id '
            'WHERE x.source_alias=l.source_alias AND x.target_alias=l.target_alias) '
            'ORDER BY l.source_alias,l.target_alias LIMIT ?', (limit,))))

    @snapshot_read
    def edge_selections(self, edges, limit):
        result = []
        for source, target in edges:
            result.extend((row[0], source, target) for row in self.conn.execute(
                'SELECT generation_id FROM group_generation_lineage WHERE source_alias=? AND target_alias=? '
                'ORDER BY generation_id LIMIT ?', (source, target, limit - len(result))))
            if len(result) == limit:
                break
        return result

    @snapshot_read
    def published_generations(self, limit):
        return [row[0] for row in self.conn.execute('SELECT generation_id FROM group_publications '
                                                   'ORDER BY generation_id DESC LIMIT ?', (limit,))]

    @snapshot_read
    def alias_selected_elsewhere(self, generation):
        return self.conn.execute('SELECT 1 FROM group_version_aliases a WHERE a.first_generation_id=? AND EXISTS('
            'SELECT 1 FROM generation_groups x WHERE x.external_version=a.external_version AND x.generation_id<>?) '
            'LIMIT 1', (generation, generation)).fetchone() is not None

    @snapshot_read
    def advance_walk(self, roots):
        return group_retention_walk.advance(self.conn, [(ROOTS[name][0] + (' INDEXED BY ' + ROOT_INDEXES[name]
                                                         if name in ROOT_INDEXES else ''), ROOTS[name][1], expand)
                                                       for name, expand in roots])

    @snapshot_read
    def walk_expired(self, batch):
        return group_retention_walk.expired(self.conn, batch)

    @synchronized
    def apply(self, selected):
        with self.conn.atomic(join=True):
            group_retention.apply(self.conn, selected)
