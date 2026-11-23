"""Give ready recommendation file receipts the exact chunk coordinates."""
import re

from .. import recommendation_chunks as chunks, worker_settings
from ..document_paths import record_id
from ..recommendation_content import FILE_IDENTITY, FILE_KIND
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .runner import PreparationChanged

NAME = 'own_recommendation_chunks_on_ready_receipts'
RETIRED = chunks.OWNER
RECEIPTS = chunks.RECEIPTS
REFERENCE = re.compile(r'\b' + RETIRED + r'\b')
POINT = 'uq_recommendation_chunk_receipt_key'
RANK = 'uq_recommendation_chunk_receipt_rank'
BY_ID = 'uq_recommendation_chunk_receipt_id'
CURSOR = 'prepare_recommendation_chunk_receipts_after_id'
PAGE_SIZE = 128


def _objects(conn):
    return [tuple(row) for row in conn.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type,name')
        if row[1] == chunks.RECEIPTS or row[2] == RETIRED or REFERENCE.search(row[3])]


def predecessor_objects(conn):
    return [sql for _, _, _, sql in _objects(conn)]


def _copy_page(conn, after):
    receipts = RECEIPTS
    page = conn.execute(f'''SELECT s.id,s.generation_id,s.surface,s.chunk_key,s.filename,s.content_digest,
            s.byte_size,s.built_at,s.expires_at,s.pool_id,s.first_rank,s.last_rank
        FROM {RETIRED} s LEFT JOIN {receipts} r ON r.chunk_id=s.id
        WHERE s.id>? AND r.id IS NULL ORDER BY s.id LIMIT ?''', (after, PAGE_SIZE)).fetchall()
    for identity, generation, surface, key, filename, digest, size, built, expires, pool, first, last in page:
        attachment = identity, generation, filename, built, expires, pool, first, last
        file_identity = record_id(filename)
        if file_identity is not None:
            owner = conn.execute(f'''SELECT id,chunk_id FROM {receipts}
                WHERE kind='{FILE_KIND}' AND {FILE_IDENTITY}=? AND owner_id=json_array(?,?,?,?)
                  AND file_status='ready' AND content_digest=? AND byte_size=?''',
                (file_identity, file_identity, chunks.OWNER, surface, key, digest, size)).fetchone()
            if owner is None:
                raise MigrationError(f'Recommendation chunk {identity} did not match its ready file receipt')
            if owner[1] is None:
                conn.execute(f'UPDATE {receipts} SET {chunks.ASSIGN} WHERE id=?', (*attachment, owner[0]))
                continue
        # Content-addressed files and shared historical coordinates already exist.
        # Admit their exact locators without rewriting their bytes or names.
        marks = ','.join('?' for _ in chunks.ATTACHMENT)
        conn.execute(f'''INSERT INTO {receipts}(kind,owner_id,content_digest,byte_size,file_status,{','.join(chunks.ATTACHMENT)})
            VALUES (?,json_array(?,?,?,?),?,?,'ready',{marks})''',
            (chunks.LEGACY_FILE_KIND, file_identity, chunks.OWNER, surface, key, digest, size, *attachment))
    boundary = page[-1][0] if page else conn.execute(f'SELECT COALESCE(max(id),0) FROM {RETIRED}').fetchone()[0]
    worker_settings.save(conn, CURSOR, str(boundary))
    return bool(page)


def prepare(conn, version):
    while True:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone():
                return
            if not definitions(conn, (BY_ID,)):
                if conn.execute(f'SELECT 1 FROM {RETIRED} WHERE id<=0 LIMIT 1').fetchone():
                    raise MigrationError('Recommendation chunks require positive IDs')
                _columns(conn)
                _indexes(conn)
                worker_settings.save(conn, CURSOR, '0')
            elif not _copy_page(conn, worker_settings.number(conn, CURSOR)):
                return


def install(conn, _context):
    after = worker_settings.read(conn, CURSOR)
    if after is None or conn.execute(f'SELECT 1 FROM {RETIRED} WHERE id>? LIMIT 1', (int(after),)).fetchone():
        raise PreparationChanged('Recommendation chunk preparation is incomplete')
    dependents = [(name, sql) for kind, name, owner, sql in _objects(conn)
                  if owner != RETIRED and kind == 'trigger' and REFERENCE.search(sql)]
    for name, _ in dependents:
        conn.execute(f'DROP TRIGGER {name}')
    serial = conn.execute('SELECT max(COALESCE((SELECT seq FROM sqlite_sequence WHERE name=?),0),'
                          f'COALESCE((SELECT max(id) FROM {RETIRED}),0))', (RETIRED,)).fetchone()[0]
    if serial:
        worker_settings.save(conn, chunks.SERIAL, str(serial))
    conn.execute(f'DROP TABLE {RETIRED}')
    worker_settings.save(conn, CURSOR, None)
    _directory(conn)
    for _, sql in dependents:
        conn.execute(REFERENCE.sub(chunks.VIEW, sql))
    _guards(conn)


def _columns(conn):
    receipts = chunks.RECEIPTS
    conn.execute(f'ALTER TABLE {receipts} ADD COLUMN chunk_id INTEGER')
    conn.execute(f'ALTER TABLE {receipts} ADD COLUMN chunk_generation_id INTEGER REFERENCES recommendation_generations(id)')
    conn.execute(f'ALTER TABLE {receipts} ADD COLUMN chunk_filename TEXT')
    conn.execute(f'ALTER TABLE {receipts} ADD COLUMN chunk_built_at REAL')
    conn.execute(f'ALTER TABLE {receipts} ADD COLUMN chunk_expires_at REAL')
    conn.execute(f'ALTER TABLE {receipts} ADD COLUMN chunk_pool_id INTEGER REFERENCES recommendation_pools(id)')
    conn.execute(f'ALTER TABLE {receipts} ADD COLUMN chunk_first_rank INTEGER')
    empty = ' AND '.join(f'{column} IS NULL' for column in chunks.ATTACHMENT)
    conn.execute(f'''ALTER TABLE {receipts} ADD COLUMN chunk_last_rank INTEGER CHECK(
      CASE WHEN chunk_id IS NULL THEN {empty} ELSE COALESCE(
        typeof(chunk_id)='integer' AND chunk_id>0 AND chunk_generation_id IS NOT NULL AND length(chunk_filename)>0
        AND kind IN ('{FILE_KIND}','{chunks.LEGACY_FILE_KIND}') AND file_status='ready'
        AND owner_id->>1='{chunks.OWNER}' AND typeof({chunks.SURFACE})='text' AND typeof({chunks.KEY})='text'
        AND byte_size BETWEEN 1 AND 1048576 AND typeof(chunk_built_at) IN ('real','integer')
        AND abs(chunk_built_at)<=1.7976931348623157e308 AND (chunk_expires_at IS NULL OR
            typeof(chunk_expires_at) IN ('real','integer') AND abs(chunk_expires_at)<=1.7976931348623157e308)
        AND (chunk_pool_id IS NULL AND chunk_first_rank IS NULL AND chunk_last_rank IS NULL OR
            chunk_pool_id IS NOT NULL AND typeof(chunk_first_rank)='integer' AND chunk_first_rank>=0
            AND typeof(chunk_last_rank)='integer' AND chunk_last_rank>=chunk_first_rank),0) END)''')


def _indexes(conn):
    receipts = chunks.RECEIPTS
    surface, key = chunks.SURFACE, chunks.KEY
    conn.execute(f'CREATE UNIQUE INDEX {BY_ID} ON {receipts}(chunk_id) WHERE chunk_id IS NOT NULL')
    conn.execute(f'CREATE UNIQUE INDEX {POINT} ON {receipts}(chunk_generation_id,{surface},{key}) WHERE chunk_id IS NOT NULL')
    conn.execute(f'CREATE UNIQUE INDEX {RANK} ON {receipts}(chunk_generation_id,{surface},chunk_pool_id,chunk_first_rank) '
                 'WHERE chunk_id IS NOT NULL AND chunk_pool_id IS NOT NULL')
    conn.execute(f'CREATE INDEX ix_recommendation_chunk_receipt_pool ON {receipts}(chunk_pool_id) '
                 'WHERE chunk_id IS NOT NULL AND chunk_pool_id IS NOT NULL')
    conn.execute(f'CREATE INDEX ix_recommendation_chunk_receipt_path ON {receipts}(chunk_filename) WHERE chunk_id IS NOT NULL')


def _directory(conn):
    receipts = chunks.RECEIPTS
    surface, key = chunks.SURFACE, chunks.KEY
    conn.execute(f'''CREATE VIEW {chunks.VIEW} AS SELECT chunk_id AS id,chunk_generation_id AS generation_id,
        {surface} AS surface,{key} AS chunk_key,chunk_filename AS filename,content_digest,byte_size,1 AS format_version,
        chunk_built_at AS built_at,chunk_expires_at AS expires_at,chunk_pool_id AS pool_id,
        chunk_first_rank AS first_rank,chunk_last_rank AS last_rank FROM {receipts} WHERE chunk_id IS NOT NULL''')


def _guards(conn):
    receipts = chunks.RECEIPTS
    coordinates = ('kind', 'owner_id', 'content_digest', 'byte_size',
                   *(column for column in chunks.ATTACHMENT if column != 'chunk_filename'))
    changed = ' OR '.join(f'NEW.{column} IS NOT OLD.{column}' for column in coordinates)
    surface = "NEW.owner_id->>2"
    for event in ('INSERT', 'UPDATE'):
        update = event == 'UPDATE'
        previous = f'AND (OLD.chunk_id IS NULL OR {changed})' if update else ''
        other = 'AND o.id IS NOT OLD.chunk_id' if update else ''
        conn.execute(f'''CREATE TRIGGER admit_recommendation_chunk_receipt_{event.lower()} BEFORE {event} ON {receipts}
            WHEN NEW.chunk_id IS NOT NULL {previous} BEGIN
              SELECT RAISE(ABORT,'Recommendation publication content is immutable') WHERE NOT EXISTS(
                  SELECT 1 FROM recommendation_generations g JOIN recommendation_generation_surfaces s ON s.generation_id=g.id
                  WHERE g.id=NEW.chunk_generation_id AND s.surface={surface} AND g.status='building');
              SELECT RAISE(ABORT,'Display chunk requires disjoint ranks in its selected pool') WHERE NEW.chunk_pool_id IS NOT NULL
                AND (NOT EXISTS(SELECT 1 FROM recommendation_surface_pools m JOIN recommendation_pools p ON p.id=m.pool_id
                    WHERE m.generation_id=NEW.chunk_generation_id AND m.surface={surface} AND m.pool_id=NEW.chunk_pool_id
                      AND NEW.chunk_last_rank<p.eligible_count) OR EXISTS(SELECT 1 FROM {chunks.VIEW} o
                    WHERE o.generation_id=NEW.chunk_generation_id AND o.surface={surface} AND o.pool_id=NEW.chunk_pool_id
                      AND o.first_rank<=NEW.chunk_last_rank AND o.last_rank>=NEW.chunk_first_rank {other}));
            END''')
    for event, predicate, allowed in (
            ('DELETE', '', "g.status IN ('building','retiring')"),
            ('UPDATE', f'AND ({changed})', "g.status='building' OR (g.status='retiring' AND NEW.chunk_id IS NULL)")):
        conn.execute(f'''CREATE TRIGGER frozen_recommendation_chunk_receipt_{event.lower()} BEFORE {event} ON {receipts}
            WHEN OLD.chunk_id IS NOT NULL {predicate} AND EXISTS(SELECT 1 FROM recommendation_generations g
                WHERE g.id=OLD.chunk_generation_id AND NOT ({allowed}))
            BEGIN SELECT RAISE(ABORT,'Recommendation publication content is immutable'); END''')
    for event, predicate in (('DELETE', ''), ('UPDATE OF generation_id,surface',
                             'AND (NEW.generation_id IS NOT OLD.generation_id OR NEW.surface IS NOT OLD.surface)')):
        conn.execute(f'''CREATE TRIGGER retained_recommendation_chunk_surface_{event.split()[0].lower()}
            BEFORE {event} ON recommendation_generation_surfaces
            WHEN EXISTS(SELECT 1 FROM {chunks.VIEW} WHERE generation_id=OLD.generation_id AND surface=OLD.surface)
              {predicate} BEGIN SELECT RAISE(ABORT,'FOREIGN KEY constraint failed'); END''')


def validate(conn):
    names = {chunks.VIEW, POINT, RANK, 'frozen_recommendation_chunk_receipt_update'}
    if set(definitions(conn, (*names, RETIRED))) != names:
        raise MigrationError('Recommendation chunks were not moved to their file receipts')
