"""Keep the selected native observations on the provider source."""
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .runner import PreparationChanged


NAME = 'fold_musical_observation_selection'
SOURCE = 'rec_musical_sources'
RETIRED = 'musical_source_observations'
COLUMN = "selected_observations TEXT NOT NULL DEFAULT '{}' CHECK(json_type(selected_observations) IS 'object')"
PREPARATION = 'ix_prepare_musical_observation_sources'
ENTITY_INDEX = 'ix_selected_musical_observation_entity'
PAGE_SIZE = 512
SELECTED = "selected_observations<>'{}'"
BOUNDARY = f'SELECT max(source_key) FROM {SOURCE} INDEXED BY {PREPARATION} WHERE {SELECTED}'
DOCUMENT = (f'(SELECT json_group_object(field,observation_id) FROM {RETIRED} '
            f'WHERE source_key={SOURCE}.source_key)')


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        'SELECT sql FROM sqlite_schema WHERE tbl_name IN (?,?) AND sql IS NOT NULL ORDER BY type,name',
        (SOURCE, RETIRED))]


def preparation_indexes():
    yield f'CREATE INDEX {PREPARATION} ON {SOURCE}(source_key) WHERE {SELECTED}'
    yield f'CREATE INDEX {ENTITY_INDEX} ON {SOURCE}(provider,scope,entity_id) WHERE {SELECTED}'


def _remaining(conn):
    boundary, = conn.execute(BOUNDARY).fetchone()
    if boundary is None:
        return f'FROM {RETIRED}', ()
    return f'FROM {RETIRED} WHERE source_key>?', (boundary,)


def _prepare_page(conn, version):
    if conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone():
        return False
    if not definitions(conn, (PREPARATION,)):
        conn.execute(f'ALTER TABLE {SOURCE} ADD COLUMN {COLUMN}')
        for sql in preparation_indexes():
            conn.execute(sql)
        return True
    remaining, args = _remaining(conn)
    count = conn.execute(f'''UPDATE {SOURCE} SET selected_observations={DOCUMENT}
        WHERE source_key IN (SELECT DISTINCT source_key {remaining} ORDER BY source_key LIMIT ?)''',
        (*args, PAGE_SIZE)).rowcount
    if not count and conn.execute(f'SELECT 1 {remaining} LIMIT 1', args).fetchone():
        raise MigrationError('Selected musical observations are missing their source')
    return bool(count)


def prepare(conn, version):
    while True:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if not _prepare_page(conn, version):
                return


def _guards():
    for name, event in (('insert', 'INSERT'), ('update', 'UPDATE OF selected_observations,provider,scope,entity_id')):
        yield f'''CREATE TRIGGER musical_observation_selection_{name} BEFORE {event} ON {SOURCE}
            WHEN EXISTS(SELECT 1 FROM json_each(NEW.selected_observations) f
                LEFT JOIN native_metadata_observations o ON o.id=f.atom AND o.field=f.key
                LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id AND d.provider=NEW.provider
                    AND d.provider_entity_type=NEW.scope AND d.provider_entity_id=NEW.entity_id
                GROUP BY f.key HAVING count(*)<>1 OR count(d.id)<>1 OR min(f.type)<>'integer')
            BEGIN SELECT RAISE(ABORT,'Musical source selection has a different provider entity or field'); END'''
    yield f'''CREATE TRIGGER musical_observation_selection_source_delete BEFORE DELETE ON {SOURCE}
        WHEN OLD.selected_observations<>'{{}}'
        BEGIN SELECT RAISE(ABORT,'Source still selects a musical observation'); END'''
    yield f'''CREATE TRIGGER musical_observation_selection_source_key BEFORE UPDATE OF source_key ON {SOURCE}
        WHEN OLD.source_key IS NOT NEW.source_key AND OLD.selected_observations<>'{{}}'
        BEGIN SELECT RAISE(ABORT,'Source still selects a musical observation'); END'''
    yield f'''CREATE TRIGGER retained_native_observation BEFORE DELETE ON native_metadata_observations
        WHEN EXISTS(SELECT 1 FROM metadata_deliveries d JOIN {SOURCE} s
            ON (s.provider,s.scope,s.entity_id)=(d.provider,d.provider_entity_type,d.provider_entity_id)
            WHERE d.id=OLD.delivery_id AND s.selected_observations<>'{{}}'
                AND s.selected_observations->>OLD.field=OLD.id)
        BEGIN SELECT RAISE(ABORT,'A source still selects this musical observation'); END'''


def install(conn, _context):
    if not definitions(conn, (PREPARATION,)):
        raise PreparationChanged()
    remaining, args = _remaining(conn)
    if conn.execute(f'SELECT 1 {remaining} LIMIT 1', args).fetchone():
        raise PreparationChanged()
    conn.execute(f'DROP INDEX {PREPARATION}')
    for sql in _guards():
        conn.execute(sql)
    conn.execute(f'DROP TABLE {RETIRED}')


def validate(conn):
    installed = definitions(conn, (SOURCE, RETIRED, PREPARATION))
    if set(installed) != {SOURCE} or COLUMN not in installed[SOURCE]:
        raise MigrationError('Musical observation selection was not folded into its source')
