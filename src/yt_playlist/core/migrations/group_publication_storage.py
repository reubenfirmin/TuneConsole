"""Keep group membership and staging facts with their existing owners."""
import re

from .. import group_preview_work, recommendation_revalidations
from ..schema_contract import MigrationError
from ..schema_inspection import definitions, objects
from .account_storage import _preserve_sequence, _sequence
from .track_source_cutover import _replace_table


NAME = 'compact_group_publication_storage'
MARKER = 'ix_group_member_raw_key'
REMOVED = ('group_build_versions', 'group_file_writes', 'group_graph', 'group_unknown',
           'unresolved_group_contexts', 'unresolved_group_graph', 'unresolved_group_members',
           'unresolved_group_unknown')
FK_OWNERS = ('group_versions', 'group_members', 'group_evidence_owners', 'recommendation_pools', *REMOVED)
PREVIEW_SOURCES = frozenset(('group_members', 'recommendation_pools'))
MEMBER_OBJECTS = ('ix_group_member_input', 'ix_group_member_recording', 'immutable_group_members',
                  'sealed_group_members_delete', 'sealed_group_members_insert')
RETIRED_POOL_INDEX = 'ix_recommendation_pool_group'
BUILD_COLUMN = 'build_generation_id INTEGER'
BUILD_REFERENCE = BUILD_COLUMN + ' REFERENCES group_builds(generation_id)'
STAGED_VERSIONS = 'FROM group_versions WHERE build_generation_id=NEW.generation_id'
RETIRED_STAGED_VERSIONS = 'FROM group_build_versions WHERE generation_id=NEW.generation_id'
REPAIR_NAME = 'repair_installed_group_publication_guard'
BUILD_GUARDS = ('staged_group_version_build_insert', 'staged_group_version_build_update',
                'retained_group_build_delete', 'retained_group_build_update')
PREDECESSOR_OBJECTS = {
    'group_versions', 'group_members', 'group_evidence_owners', 'recommendation_pools',
    'complete_group_version_alias', 'complete_group_publication', 'retired_proposal_group_references',
    'immutable_recommendation_pool',
    *(f'group_preview_changed_{table}_update' for table in PREVIEW_SOURCES),
}
MEMBERS = '''CREATE TABLE _migration_group_members (
    group_version_id INTEGER NOT NULL REFERENCES group_versions(id),
    recording_id INTEGER REFERENCES recordings(id),
    similarity REAL NOT NULL CHECK(similarity BETWEEN 0 AND 1),
    support REAL NOT NULL CHECK(support BETWEEN 0 AND 1),
    input_id INTEGER,
    raw_key TEXT,
    input_digest TEXT CHECK(input_digest IS NULL OR
        (length(input_digest)=64 AND input_digest NOT GLOB '*[^0-9a-f]*')),
    CHECK((recording_id IS NOT NULL AND raw_key IS NULL AND input_digest IS NULL)
        OR (recording_id IS NULL AND raw_key IS NOT NULL AND input_id IS NULL)),
    UNIQUE(group_version_id,recording_id),
    FOREIGN KEY(input_id,recording_id) REFERENCES group_recording_inputs(id,recording_id))'''


def _stage(conn, prior):
    # Build ownership is temporary. Clearing it is part of sealing the version
    # or fencing a failed build. A published version has no lease reference.
    conn.execute('ALTER TABLE group_versions ADD COLUMN build_generation_id INTEGER')
    conn.execute("ALTER TABLE group_versions ADD COLUMN build_position INTEGER "
                 "CHECK(build_position IS NULL OR (typeof(build_position)='integer' AND build_position>=0))")
    conn.execute('''ALTER TABLE group_versions ADD COLUMN build_external_version TEXT CHECK(
        (build_generation_id IS NULL AND build_position IS NULL AND build_external_version IS NULL)
        OR (build_generation_id IS NOT NULL AND build_position IS NOT NULL
            AND build_external_version IS NOT NULL AND length(build_external_version)>0))''')
    conn.execute('''UPDATE group_versions AS v SET (build_generation_id,build_position,build_external_version)=
        (SELECT generation_id,position,external_version FROM group_build_versions WHERE group_version_id=v.id)
        WHERE EXISTS(SELECT 1 FROM group_build_versions WHERE group_version_id=v.id)''')
    for column in ('build_position', 'build_external_version'):
        conn.execute(f'CREATE UNIQUE INDEX uq_group_version_{column} ON group_versions(build_generation_id,{column}) '
                     'WHERE build_generation_id IS NOT NULL')
    _install_build_guards(conn)
    conn.execute('DROP TRIGGER complete_group_publication')
    conn.execute(prior['complete_group_publication'].replace(RETIRED_STAGED_VERSIONS, STAGED_VERSIONS))


def _install_build_guards(conn):
    for event in ('INSERT', 'UPDATE OF build_generation_id'):
        action = event.split()[0].lower()
        conn.execute(f'''CREATE TRIGGER staged_group_version_build_{action} BEFORE {event} ON group_versions
            WHEN NEW.build_generation_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM group_builds WHERE generation_id=NEW.build_generation_id AND ready=0)
            BEGIN SELECT RAISE(ABORT,'Staged musical group version requires an active build'); END''')
    for event in ('DELETE', 'UPDATE OF generation_id'):
        action = event.split()[0].lower()
        conn.execute(f'''CREATE TRIGGER retained_group_build_{action} BEFORE {event} ON group_builds
            WHEN EXISTS(SELECT 1 FROM group_versions WHERE build_generation_id=OLD.generation_id)
            BEGIN SELECT RAISE(ABORT,'Musical group build still owns staged versions'); END''')


def installed_with_build_reference(conn):
    table = objects(conn, ('group_versions',)).get('group_versions')
    return table is not None and BUILD_REFERENCE in table[1]


def repair_group_publication_guard(conn, _context):
    if not installed_with_build_reference(conn):
        return
    prior = definitions(conn, ('group_versions', 'complete_group_publication'))
    owned_sql = [sql for (sql,) in conn.execute(
        "SELECT sql FROM main.sqlite_schema WHERE tbl_name='group_versions' "
        "AND sql IS NOT NULL AND type IN ('index','trigger') ORDER BY type,name")]
    highwater = _sequence(conn, 'group_versions')
    _replace_table(conn, 'group_versions', prior['group_versions'].replace(BUILD_REFERENCE, BUILD_COLUMN))
    _preserve_sequence(conn, 'group_versions', highwater)
    for sql in owned_sql:
        conn.execute(sql)
    conn.execute('DROP TRIGGER complete_group_publication')
    conn.execute(prior['complete_group_publication'].replace(RETIRED_STAGED_VERSIONS, STAGED_VERSIONS))
    _install_build_guards(conn)


def validate_group_publication_guard(conn):
    actual = definitions(conn, ('complete_group_publication', *BUILD_GUARDS))
    if (installed_with_build_reference(conn) or any(name not in actual for name in BUILD_GUARDS)
            or all(guard not in actual.get('complete_group_publication', '')
                   for guard in (STAGED_VERSIONS, 'FROM group_versions WHERE build_generation_id=NEW.id'))):
        raise MigrationError('Incompatible musical group publication guards')


def _members(conn, prior):
    conn.execute('DROP TRIGGER complete_group_version_alias')
    conn.execute(MEMBERS)
    conn.execute('''INSERT INTO _migration_group_members(group_version_id,recording_id,similarity,support,input_id)
        SELECT group_version_id,recording_id,similarity,support,input_id FROM group_members''')
    conn.execute('''INSERT INTO _migration_group_members(group_version_id,raw_key,similarity,support,input_digest)
        SELECT group_version_id,raw_key,similarity,support,input_digest FROM unresolved_group_members''')
    conn.execute('DROP TABLE group_members')
    conn.execute('ALTER TABLE _migration_group_members RENAME TO group_members')
    conn.execute(f'CREATE UNIQUE INDEX {MARKER} ON group_members(group_version_id,raw_key) WHERE raw_key IS NOT NULL')
    for name in MEMBER_OBJECTS:
        conn.execute(prior[name])
    conn.execute(prior['complete_group_version_alias']
        .replace('+(SELECT count(*) FROM unresolved_group_members WHERE group_version_id=v.id)', '')
        .replace(' OR EXISTS(SELECT 1 FROM unresolved_group_members WHERE group_version_id=v.id)', ''))


def _contexts(conn, prior):
    conn.execute('DROP TRIGGER immutable_group_evidence_owners')
    conn.execute('DROP TRIGGER retired_proposal_group_references')
    conn.execute('ALTER TABLE group_evidence_owners ADD COLUMN unresolved_context_source TEXT')
    conn.execute('''ALTER TABLE group_evidence_owners ADD COLUMN unresolved_context_reason TEXT CHECK(
        (unresolved_context_source IS NULL AND unresolved_context_reason IS NULL)
        OR (unresolved_context_source IS NOT NULL AND unresolved_context_reason IS NOT NULL
            AND unresolved_context_reason IN ('missing_context','invalid_context','missing_offer','invalid_checkpoint')))''')
    conn.execute('''UPDATE group_evidence_owners AS o SET (unresolved_context_source,unresolved_context_reason)=
        (SELECT source_context,reason FROM unresolved_group_contexts WHERE kind=o.kind AND external_key=o.external_key)
        WHERE EXISTS(SELECT 1 FROM unresolved_group_contexts WHERE kind=o.kind AND external_key=o.external_key)''')
    conn.execute(prior['immutable_group_evidence_owners'])
    conn.execute(re.sub(r'DELETE FROM unresolved_group_contexts [^;]*; ', '',
                        prior['retired_proposal_group_references'], count=1))


def _pools(conn, prior):
    conn.execute('DROP TRIGGER complete_recommendation_pool_selection')
    conn.execute('DROP TRIGGER immutable_recommendation_pool')
    conn.execute('DROP TRIGGER group_preview_changed_recommendation_pools_update')
    conn.execute('DROP INDEX ' + RETIRED_POOL_INDEX)
    conn.execute('ALTER TABLE recommendation_pools DROP COLUMN group_version_id')
    conn.execute(prior['immutable_recommendation_pool'].replace(',group_version_id', ''))
    recommendation_revalidations.install_guards(conn, group_scoped=False)


def install(conn, _context):
    prior = definitions(conn, (*MEMBER_OBJECTS, 'complete_group_version_alias', 'complete_group_publication',
        'immutable_group_evidence_owners', 'retired_proposal_group_references', 'immutable_recommendation_pool'))
    _stage(conn, prior)
    _members(conn, prior)
    _contexts(conn, prior)
    _pools(conn, prior)
    for table in REMOVED:
        conn.execute('DROP TABLE ' + table)
    group_preview_work.install_guards(conn, sources=PREVIEW_SOURCES)
    conn.execute('UPDATE group_preview_work SET invalid=1 WHERE singleton=1 AND invalid=0')


def validate(conn):
    observed = objects(conn, (*REMOVED, MARKER, RETIRED_POOL_INDEX))
    if MARKER not in observed or observed.keys() - {MARKER}:
        raise MigrationError('Incompatible compact musical group publication storage')
