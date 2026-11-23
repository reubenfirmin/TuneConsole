"""Put scalar field credits on their attempt and current-state owners."""
from dataclasses import dataclass

from ..db_checkpoint import deferred_autocheckpoint
from ..metadata_contract import PROVIDER_FIELDS
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .runner import PreparationChanged


NAME = 'inline_provider_field_links'
OWNERS = ('provider_attempts', 'enrichment_provider_state')
RETIRED = ('provider_attempt_fields', 'provider_state_fields')
_COLUMN = "field_links TEXT NOT NULL DEFAULT '{}' CHECK(json_type(field_links) IS 'object')"
_FIELDS = ','.join(repr(field) for field in PROVIDER_FIELDS if field != 'artist_credits')
_ARTIFACTS = "'art','isrc','mb_release_ids','cover_url'"
PAGE_SIZE = 512


def _key(columns, table=None):
    prefix = f'{table}.' if table else ''
    return '(' + ','.join(prefix + column for column in columns) + ')'


@dataclass(frozen=True)
class FieldLinks:
    owner: str
    source: str
    owner_keys: tuple[str, ...]
    source_keys: tuple[str, ...]
    value: str

    @property
    def index(self):
        return f'ix_prepare_{self.source}'

    def boundary(self):
        order = ','.join(f'{key} DESC' for key in self.owner_keys)
        return (f'SELECT {",".join(self.owner_keys)} FROM {self.owner} INDEXED BY {self.index} '
                f"WHERE field_links<>'{{}}' ORDER BY {order} LIMIT 1")

    def document(self):
        return (f'SELECT json_group_object(f.field,{self.value}) FROM {self.source} f '
                f'WHERE {_key(self.source_keys, "f")}={_key(self.owner_keys, self.owner)}')


LINKS = (
    FieldLinks(OWNERS[0], RETIRED[0], ('id',), ('attempt_id',), 'f.observation_id'),
    FieldLinks(OWNERS[1], RETIRED[1], ('recording_id', 'provider'), ('recording_id', 'provider'),
               'json_array(f.delivery_id,f.observation_id)'),
)
PREPARATION = LINKS[0].index


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE tbl_name IN (?,?,?,?) '
        'AND sql IS NOT NULL ORDER BY type,name', (*OWNERS, *RETIRED))]


def _observation(reference, delivery):
    return f'''SELECT 1 FROM metadata_observations o
        WHERE o.id={reference} AND o.delivery_id={delivery} AND o.recording_id=NEW.recording_id
        AND o.field=CASE f.key WHEN 'year' THEN 'mb_year' ELSE f.key END'''


def _guards():
    attempt = f'''f.key NOT IN ({_FIELDS}) OR
        CASE f.type WHEN 'null' THEN f.key NOT IN ({_ARTIFACTS}) WHEN 'integer' THEN
            NOT EXISTS({_observation('f.atom', 'NEW.delivery_id')}) ELSE 1 END'''
    state = f'''f.key NOT IN ({_FIELDS}) OR CASE WHEN f.type<>'array' THEN 1 ELSE
        json_array_length(f.value)<>2 OR json_type(f.value,'$[0]') IS NOT 'integer' OR
        NOT EXISTS(SELECT 1 FROM metadata_deliveries d WHERE d.id=f.value->>0 AND d.provider=NEW.provider
            AND (d.input_recording_id=NEW.recording_id OR
                (d.original_owner IN ('provider_attempts','enrichment_provider_state')
                    AND d.original_recording_id=NEW.recording_id))) OR
        CASE json_type(f.value,'$[1]') WHEN 'null' THEN f.key NOT IN ({_ARTIFACTS}) WHEN 'integer' THEN
            NOT EXISTS({_observation('f.value->>1', 'f.value->>0')}) ELSE 1 END END'''
    for suffix, attempt_event, state_event in (
        ('insert', 'INSERT', 'INSERT'),
        ('update', 'UPDATE OF field_links,recording_id,delivery_id', 'UPDATE OF field_links,recording_id,provider'),
    ):
        for name, owner, event, invalid in (
            ('attempt', OWNERS[0], attempt_event, attempt), ('state', OWNERS[1], state_event, state),
        ):
            yield f'''CREATE TRIGGER provider_{name}_links_{suffix} BEFORE {event} ON {owner}
                WHEN EXISTS(SELECT 1 FROM json_each(NEW.field_links) f WHERE {invalid})
                BEGIN SELECT RAISE(ABORT,'Provider field has a different subject, delivery or field'); END'''
    yield '''CREATE TRIGGER provider_links_observation_delete BEFORE DELETE ON metadata_observations
        WHEN EXISTS(SELECT 1 FROM provider_attempts a,json_each(a.field_links) f
            WHERE a.recording_id=OLD.recording_id AND a.delivery_id=OLD.delivery_id AND f.atom=OLD.id)
        OR EXISTS(SELECT 1 FROM enrichment_provider_state s,json_each(s.field_links) f
            WHERE s.recording_id=OLD.recording_id AND f.value->>1=OLD.id)
        BEGIN SELECT RAISE(ABORT,'Provider field still references this observation'); END'''
    yield '''CREATE TRIGGER provider_links_delivery_delete BEFORE DELETE ON metadata_deliveries
        WHEN EXISTS(SELECT 1 FROM enrichment_provider_state s,json_each(s.field_links) f
            WHERE s.recording_id IN (OLD.input_recording_id,OLD.original_recording_id)
                AND s.provider=OLD.provider AND f.value->>0=OLD.id)
        BEGIN SELECT RAISE(ABORT,'Provider field still references this delivery'); END'''


def preparation_indexes():
    for links in LINKS:
        owner_keys = ','.join(links.owner_keys)
        yield f"CREATE INDEX {links.index} ON {links.owner}({owner_keys}) WHERE field_links<>'{{}}'"


def _prepare_storage(conn):
    for links in LINKS:
        conn.execute(f'ALTER TABLE {links.owner} ADD COLUMN {_COLUMN}')
    for sql in preparation_indexes():
        conn.execute(sql)


def _remaining(conn, links):
    boundary = conn.execute(links.boundary()).fetchone()
    if boundary is None:
        return f'FROM {links.source}', ()
    placeholders = ','.join('?' for _ in links.source_keys)
    return f'FROM {links.source} WHERE {_key(links.source_keys)}>({placeholders})', tuple(boundary)


def _prepare_page(conn, version):
    if conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone():
        return False
    if not definitions(conn, (PREPARATION,)):
        _prepare_storage(conn)
        return True
    for links in LINKS:
        remaining, args = _remaining(conn, links)
        source_keys = ','.join(links.source_keys)
        if conn.execute(f'''UPDATE {links.owner} SET field_links=({links.document()})
            WHERE {_key(links.owner_keys)} IN (SELECT DISTINCT {source_keys} {remaining}
                ORDER BY {source_keys} LIMIT ?)''', (*args, PAGE_SIZE)).rowcount:
            return True
        if conn.execute(f'SELECT 1 {remaining} LIMIT 1', args).fetchone():
            raise MigrationError('Provider field links are missing their owner: ' + links.source)
    return False


def prepare(conn, version):
    wal = conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
    with deferred_autocheckpoint(conn, wal):
        while True:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                if not _prepare_page(conn, version):
                    return


def install(conn, _context):
    if not definitions(conn, (PREPARATION,)):
        raise PreparationChanged()
    for links in LINKS:
        remaining, args = _remaining(conn, links)
        if conn.execute(f'SELECT 1 {remaining} LIMIT 1', args).fetchone():
            raise PreparationChanged()
    for links in LINKS:
        conn.execute(f'DROP INDEX {links.index}')
    for sql in _guards():
        conn.execute(sql)
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')


def validate(conn):
    installed = definitions(conn, (*OWNERS, *RETIRED))
    if (any(table in installed for table in RETIRED)
            or any(_COLUMN not in installed.get(owner, '') for owner in OWNERS)):
        raise MigrationError('Incompatible inline provider field links')
