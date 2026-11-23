"""Keep each ordered artist credit together and retire redundant artist storage."""
from .. import embedding_files, legacy_journals
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .retired_catalog import _preserve_sequence
from .runner import PreparationChanged


NAME = 'compact_artist_details'
INLINE_VECTORS = 'artist_embeddings'
IDENTITY_EVIDENCE = 'artist_identity_evidence'
MEMBERS = 'artist_credit_members'
RETIRED_TABLES = (INLINE_VECTORS, IDENTITY_EVIDENCE, MEMBERS)
RETIRED_TRIGGERS = (
    'artist_embedding_files_file_basis_insert', 'artist_embedding_files_file_basis_update',
    'retire_artist_embeddings_insert', 'retire_artist_embeddings_update',
    'complete_credit_observation', 'complete_artist_credit', 'private_artist_credit',
)
CREDIT_DEFINITION = '''
CREATE TABLE artist_credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_credit TEXT NOT NULL,
    content_digest TEXT NOT NULL UNIQUE
        CHECK(length(content_digest)=64 AND content_digest NOT GLOB '*[^0-9a-f]*'),
    members_json TEXT NOT NULL
        CHECK(json_valid(members_json) AND json_type(members_json)='array'
              AND json_array_length(members_json) BETWEEN 1 AND 128)
)
'''.strip()
GUARDS = {
    'immutable_artist_credit': '''
CREATE TRIGGER immutable_artist_credit BEFORE UPDATE ON artist_credits
BEGIN SELECT RAISE(ABORT,'Artist credit content is immutable'); END
'''.strip(),
    'owned_artist_credit_members': '''
CREATE TRIGGER owned_artist_credit_members BEFORE INSERT ON artist_credits
WHEN EXISTS(
    SELECT 1 FROM json_each(NEW.members_json) m
    WHERE m.type<>'array' OR json_array_length(m.value)<>7
        OR json_type(m.value,'$[0]') NOT IN ('integer','null')
        OR (json_type(m.value,'$[0]')='integer'
            AND NOT EXISTS(SELECT 1 FROM artists WHERE id=json_extract(m.value,'$[0]')))
        OR json_type(m.value,'$[1]')<>'text' OR length(json_extract(m.value,'$[1]'))=0
        OR json_type(m.value,'$[6]')<>'integer' OR json_extract(m.value,'$[6]') NOT BETWEEN 0 AND 7
)
BEGIN SELECT RAISE(ABORT,'Invalid ordered artist credit member'); END
'''.strip(),
    'retained_artist_credit_identity': '''
CREATE TRIGGER retained_artist_credit_identity BEFORE DELETE ON artists
WHEN EXISTS(
    SELECT 1 FROM artist_credits c, json_each(c.members_json) m WHERE json_extract(m.value,'$[0]')=OLD.id
)
BEGIN SELECT RAISE(ABORT,'Artist identity is referenced by a credit'); END
'''.strip(),
}
MARKER = 'owned_artist_credit_members'


def predecessor_objects(conn):
    owners = ('artist_credits', *RETIRED_TABLES)
    owner_slots = ','.join('?' for _ in owners)
    trigger_slots = ','.join('?' for _ in RETIRED_TRIGGERS)
    return [sql for sql, in conn.execute(
        f'SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND '
        f'(tbl_name IN ({owner_slots}) OR name IN ({trigger_slots})) ORDER BY type,name',
        (*owners, *RETIRED_TRIGGERS))]


def _pending(conn, table):
    return (table in definitions(conn, (table,))
            and conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() is not None)


def prepare(conn, _version):
    for table, convert in (
        (INLINE_VECTORS, embedding_files.prepare_batch),
        (IDENTITY_EVIDENCE, legacy_journals.prepare_batch),
    ):
        while True:
            conn.execute('BEGIN IMMEDIATE')
            with conn:
                if not _pending(conn, table):
                    break
                convert(conn, 'artist')


def install(conn, _context):
    if any(_pending(conn, table) for table in (INLINE_VECTORS, IDENTITY_EVIDENCE)):
        raise PreparationChanged()
    for name in RETIRED_TRIGGERS:
        conn.execute(f'DROP TRIGGER {name}')
    highest = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='artist_credits'").fetchone()
    staging = 'artist_credits_next'
    conn.execute(CREDIT_DEFINITION.replace('artist_credits', staging, 1))
    conn.execute(f'''
        INSERT INTO {staging}(id,display_credit,content_digest,members_json)
        SELECT id,display_credit,content_digest,
            (SELECT json_group_array(
                json_array(artist_id,credit_text,provider,raw_id,raw_browse_id,role,presence) ORDER BY position
             ) FROM artist_credit_members WHERE credit_id=c.id)
        FROM artist_credits c WHERE complete=1
    ''')
    for table in RETIRED_TABLES:
        conn.execute(f'DROP TABLE {table}')
    conn.execute('DROP TABLE artist_credits')
    conn.execute(f'ALTER TABLE {staging} RENAME TO artist_credits')
    if highest is not None:
        _preserve_sequence(conn, 'artist_credits', highest[0])
    for sql in GUARDS.values():
        conn.execute(sql)
    legacy_journals._refresh_file_references(conn)


def validate(conn):
    found = definitions(conn, (*RETIRED_TABLES, *RETIRED_TRIGGERS, *GUARDS))
    if not GUARDS.keys() <= found.keys() or found.keys() & {*RETIRED_TABLES, *RETIRED_TRIGGERS}:
        raise MigrationError('Artist detail storage was not compacted')
