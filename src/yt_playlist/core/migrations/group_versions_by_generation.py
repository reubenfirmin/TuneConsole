"""Own group versions, members and input metadata by generation."""
import re

from .. import group_documents
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .track_source_cutover import _replace_table


NAME = 'group_versions_by_generation'
SUPERSEDED_VALIDATIONS = frozenset({73, 98, 101, 113, 117, 135})
RETIRED = ('generation_groups', 'group_version_aliases', 'group_lineage', 'group_generation_lineage',
           'group_input_manifests', 'group_manifest_inputs', 'group_preview_pages', 'group_preview_dependencies')
OWNERS = (*RETIRED, 'group_versions', 'group_generations', 'group_members', 'group_evidence_owners',
          'group_evidence_references', 'group_preparation_state')
OBSOLETE_GUARDS = {
    'complete_group_publication', 'fenced_group_lineage_retirement', 'recording_metadata_preview_invalidate',
    'retained_group_build_delete', 'retained_group_build_update', 'retained_group_publication_delete',
    'sealed_group_members_delete', 'sealed_group_members_insert', 'staged_group_version_build_insert',
    'staged_group_version_build_update',
}


def _obsolete(name):
    return name in OBSOLETE_GUARDS or name.startswith(('group_preview_changed_', 'group_retention_'))


def predecessor_objects(conn):
    selected = set(OWNERS)
    return [sql for name, table, sql in conn.execute(
        'SELECT name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type,name')
        if table in selected or _obsolete(name)]


def _metadata(conn):
    generations = conn.execute('SELECT id,input_manifest_id,metadata_filename,metadata_digest,metadata_bytes,'
                               'metadata_offset FROM group_generations ORDER BY id')
    for identity, manifest, *reference in generations:
        value = group_documents.read(*reference, 'generation')
        # The sealed manifest owns IDs and raw keys separately. A recording's
        # current catalog key may now match a historical unresolved key.
        value['recording_inputs'] = {
            f'recording:{recording_id}' if recording_id is not None else f'unresolved:{raw_key}': digest
            for recording_id, raw_key, digest in conn.execute(
                'SELECT recording_id,raw_key,input_digest FROM group_manifest_inputs WHERE manifest_id=?',
                (manifest,))
        }
        edges = [{'source': source, 'target': target, 'relation': relation,
                  'payload': group_documents.read(*evidence, 'lineage') if evidence[0] is not None else None}
                 for source, target, relation, *evidence in conn.execute(
                     'SELECT l.source_alias,l.target_alias,l.relation,l.evidence_filename,l.evidence_digest,'
                     'l.evidence_bytes,l.evidence_offset FROM group_generation_lineage x '
                     'JOIN group_lineage l USING(source_alias,target_alias) WHERE x.generation_id=? '
                     'ORDER BY l.source_alias,l.target_alias', (identity,))]
        if edges:
            value['historical_lineage'] = edges
        yield identity, value


def _versions(conn, sql):
    sql = sql[:sql.index(', build_generation_id')] + '''
        ,generation_id INTEGER NOT NULL REFERENCES group_generations(id),
        position INTEGER CHECK(position IS NULL OR (typeof(position)='integer' AND position>=0)),
        external_version TEXT NOT NULL CHECK(length(external_version)>0),
        is_origin INTEGER NOT NULL CHECK(is_origin IN (0,1)),
        complete INTEGER NOT NULL CHECK(complete IN (0,1)),
        CHECK(legacy=0 OR membership_known=0),
        CHECK(parent_group_id IS NULL OR parent_group_id<>group_id),
        UNIQUE(generation_id,position), UNIQUE(generation_id,external_version),
        UNIQUE(external_version,id))'''
    conn.execute(re.sub(r'^CREATE TABLE "?group_versions"?', 'CREATE TABLE _migration_group_versions', sql, count=1))
    # Keep the previous IDs for their primary occurrence where possible. Shared
    # content gets a membership copy for each additional generation selection.
    conn.execute('''CREATE TEMP TABLE _group_version_map AS
        WITH selections AS (
            SELECT x.group_version_id AS old_id,x.generation_id,x.position,x.external_version,
                   a.first_generation_id=x.generation_id AS is_origin,1 AS complete
            FROM generation_groups x JOIN group_version_aliases a USING(external_version)
            UNION ALL
            SELECT a.group_version_id,a.first_generation_id,NULL,a.external_version,1,1
            FROM group_version_aliases a WHERE NOT EXISTS(
                SELECT 1 FROM generation_groups x
                WHERE x.generation_id=a.first_generation_id AND x.external_version=a.external_version)
            UNION ALL
            SELECT v.id,v.build_generation_id,v.build_position,v.build_external_version,0,0
            FROM group_versions v WHERE v.build_generation_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM generation_groups x
                WHERE x.generation_id=v.build_generation_id AND x.group_version_id=v.id)
        ), ranked AS (
            SELECT *,row_number() OVER (PARTITION BY old_id ORDER BY is_origin DESC,generation_id,external_version) AS occurrence,
                   row_number() OVER (ORDER BY old_id,generation_id,external_version) AS ordinal
            FROM selections
        )
        SELECT CASE WHEN occurrence=1 THEN old_id
                    ELSE (SELECT max(id) FROM group_versions)+ordinal END AS id,
               old_id,generation_id,position,external_version,is_origin,complete
        FROM ranked''')
    conn.execute('CREATE INDEX temp._group_version_old ON _group_version_map(old_id)')
    conn.execute('CREATE INDEX temp._group_version_key ON _group_version_map(external_version,generation_id)')
    fields = ('group_id,parent_group_id,content_digest,policy_version,created_at,definition_filename,'
              'definition_digest,definition_bytes,definition_offset,leaf,legacy,membership_known,depth,size')
    conn.execute('INSERT INTO _migration_group_versions(id,' + fields +
                 ',generation_id,position,external_version,is_origin,complete) SELECT m.id,' + fields +
                 ',m.generation_id,m.position,m.external_version,m.is_origin,m.complete '
                 'FROM _group_version_map m JOIN group_versions v ON v.id=m.old_id')
    conn.execute('DROP TABLE group_versions')
    conn.execute('ALTER TABLE _migration_group_versions RENAME TO group_versions')


def install(conn, _context):
    tables = definitions(conn, OWNERS)
    changed = ('group_generations', 'group_versions', 'group_members', 'group_preparation_state',
               'group_evidence_references')
    recreate = []
    for name, table, kind, sql in conn.execute(
            "SELECT name,tbl_name,type,sql FROM sqlite_schema WHERE type IN ('index','trigger') AND sql IS NOT NULL"):
        if kind == 'trigger' and _obsolete(name):
            conn.execute(f'DROP TRIGGER "{name}"')
        elif table in changed and not _obsolete(name) and name not in (
                'ix_group_generation_manifest', 'ix_group_evidence_walk', 'ix_group_retention_generation_queue',
                'uq_group_version_build_external_version', 'uq_group_version_build_position'):
            recreate.append(sql.replace(',input_manifest_id', '').replace(
                'UPDATE OF id,group_id,parent_group_id', 'UPDATE OF id,generation_id,position,external_version,is_origin,group_id,parent_group_id'))

    # Publish the immutable metadata before replacing the SQL references.
    metadata = list((identity, tuple(group_documents.publish_many(group_documents.directory(conn),
                    [('generation', value)]))[0]) for identity, value in _metadata(conn))
    _versions(conn, tables['group_versions'])
    members = re.sub(r'^CREATE TABLE "?group_members"?', 'CREATE TABLE _migration_group_members',
                     tables['group_members'], count=1)
    conn.execute(members)
    conn.execute('INSERT INTO _migration_group_members SELECT m.id,r.recording_id,r.similarity,r.support,'
                 'r.raw_key,r.input_digest FROM group_members r JOIN _group_version_map m ON m.old_id=r.group_version_id')
    conn.execute('DROP TABLE group_members')
    conn.execute('ALTER TABLE _migration_group_members RENAME TO group_members')
    evidence = tables['group_evidence_references'].replace(
        'REFERENCES group_version_aliases(external_version,group_version_id)', 'REFERENCES group_versions(external_version,id)')
    _replace_table(conn, 'group_evidence_references', evidence, expressions={'group_version_id': '''
        CASE WHEN original.group_version_id IS NOT NULL THEN coalesce(
            (SELECT m.id FROM _group_version_map m JOIN group_evidence_owners o ON o.generation_id=m.generation_id
             WHERE o.kind=original.kind AND o.external_key=original.external_key
               AND m.external_version=original.reference_key AND m.old_id=original.group_version_id),
            (SELECT m.id FROM _group_version_map m WHERE m.external_version=original.reference_key AND m.is_origin=1))
        END'''})
    conn.execute('DROP TABLE temp._group_version_map')
    generations = re.sub(r',input_manifest_id INTEGER NOT NULL REFERENCES group_input_manifests\(id\)', '',
                         tables['group_generations']).split(', retention_revision')[0] + ')'
    _replace_table(conn, 'group_generations', generations)
    conn.executemany('UPDATE group_generations SET metadata_filename=?,metadata_digest=?,metadata_bytes=?,metadata_offset=? '
                     'WHERE id=?', ((*reference, identity) for identity, reference in metadata))
    # Interrupted builds were keyed by the old plan, including lineage. Their
    # unpublished rows can drain through normal cleanup and the worker can refit.
    conn.execute("UPDATE group_generations SET build_owner='cleanup',build_expires_at=0,build_ready=0,"
                 'build_output_digest=NULL WHERE is_published=0')
    state = tables['group_preparation_state']
    state = state[:state.index(' retention_epoch')] + state[state.index(' CHECK((phase IS NULL'):]
    _replace_table(conn, 'group_preparation_state', state)
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    for sql in recreate:
        conn.execute(sql)
    conn.execute('CREATE UNIQUE INDEX ix_group_version_origin ON group_versions(external_version) WHERE is_origin=1')
    conn.execute('CREATE UNIQUE INDEX ix_group_version_group ON group_versions(generation_id,group_id) '
                 'WHERE position IS NOT NULL')
    conn.execute('CREATE INDEX ix_group_version_key ON group_versions(external_version,generation_id)')
    conn.execute('''CREATE TRIGGER staged_group_version_insert BEFORE INSERT ON group_versions
        WHEN NOT EXISTS(SELECT 1 FROM group_generations WHERE id=NEW.generation_id
                        AND is_published=0 AND build_ready=0 AND build_owner IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'Musical group version requires an active build'); END''')
    conn.execute('''CREATE TRIGGER sealed_group_version_update BEFORE UPDATE OF complete ON group_versions
        WHEN OLD.complete=1 AND NEW.complete=0
        BEGIN SELECT RAISE(ABORT,'Musical group version is sealed'); END''')
    conn.execute('''CREATE TRIGGER complete_group_publication BEFORE UPDATE OF is_published ON group_generations
        WHEN NEW.is_published=1 AND OLD.is_published=0 AND (OLD.build_ready=0 OR EXISTS(
            SELECT 1 FROM group_versions WHERE generation_id=NEW.id AND complete=0))
        BEGIN SELECT RAISE(ABORT,'Musical group publication is incomplete'); END''')
    conn.execute('''CREATE TRIGGER sealed_group_members_insert BEFORE INSERT ON group_members
        WHEN EXISTS(SELECT 1 FROM group_versions WHERE id=NEW.group_version_id AND complete=1)
        BEGIN SELECT RAISE(ABORT,'Musical group version is sealed'); END''')
    conn.execute('''CREATE TRIGGER sealed_group_members_delete BEFORE DELETE ON group_members
        WHEN EXISTS(SELECT 1 FROM group_versions v JOIN group_generations g ON g.id=v.generation_id
                    WHERE v.id=OLD.group_version_id AND g.is_published=1)
        BEGIN SELECT RAISE(ABORT,'Musical group version is sealed'); END''')


def validate(conn):
    if definitions(conn, RETIRED):
        raise MigrationError('Retired taste group storage remains')
