"""Keep explicit runtime choices on the upload and preserve their file identities."""
import re

from .. import runtime_metadata_choices as choices, worker_settings
from ..schema_contract import MigrationError
from ..schema_inspection import definitions

NAME = 'own_runtime_metadata_choices_on_uploads'
RETIRED = 'metadata_runtime_choices'
REFERENCE = re.compile(r'\b' + RETIRED + r'\b')


def _objects(conn):
    return [tuple(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type,name")
        if row[2] in (RETIRED, 'track_sources') or REFERENCE.search(row[3])]


def predecessor_objects(conn):
    return [sql for _, _, _, sql in _objects(conn)]


def install(conn, _context):
    column = choices.COLUMN
    dependents = [(name, sql) for kind, name, owner, sql in _objects(conn)
                  if owner != RETIRED and kind == 'trigger' and REFERENCE.search(sql)]
    for name, _ in dependents:
        conn.execute(f'DROP TRIGGER {name}')
    conn.execute(f"ALTER TABLE track_sources ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}' "
                 f"CHECK(json_type({column})='object')")
    conn.execute(f'''WITH by_upload AS (
        SELECT entry_id,json_group_object(field,json_array(id,provider_id,revision,kind,text_value,
            CASE typeof(number_value) WHEN 'real' THEN json(printf('%!.26g',number_value)) ELSE number_value END,
            genre_key)) AS selection FROM {RETIRED} GROUP BY entry_id
        ) UPDATE track_sources SET {column}=by_upload.selection FROM by_upload WHERE track_sources.id=by_upload.entry_id''')
    high_water = conn.execute(f'''SELECT max(
        COALESCE((SELECT max(id) FROM {RETIRED}),0),
        COALESCE((SELECT seq FROM sqlite_sequence WHERE name=?),0),
        COALESCE((SELECT max(owner_id->>3) FROM collection_file_writes WHERE kind='runtime-metadata'
            AND owner_id->>0='choices'),0))''', (RETIRED,)).fetchone()[0]
    if high_water:
        worker_settings.save(conn, choices.SERIAL, str(high_water))
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute(f"CREATE INDEX {choices.INDEX} ON track_sources(id) WHERE {column}<>'{{}}'")
    expressions = {name: f'c.value->>{index} AS {name}' for index, name in enumerate(choices.DETAILS)}
    details = ','.join(expressions[name] for name in choices.DETAILS[1:])
    conn.execute(f'''CREATE VIEW {choices.VIEW} AS SELECT {expressions['id']},s.id AS entry_id,
        CAST(c.key AS INTEGER) AS field,{details} FROM track_sources s CROSS JOIN json_each(s.{column}) c
        WHERE s.{column}<>'{{}}' ''')
    for name, sql in dependents:
        conn.execute(REFERENCE.sub(choices.VIEW, sql))
        if name == 'track_source_metadata_choice_refresh':
            conn.execute(REFERENCE.sub(choices.VIEW, sql).replace('choice_observations', column)
                         .replace(name, 'track_source_runtime_choice_refresh'))
    _guards(conn)


def _guards(conn):
    column = choices.COLUMN
    conn.execute(f'''CREATE TRIGGER track_source_runtime_choice_validate BEFORE UPDATE OF {column} ON track_sources
        WHEN NEW.{column} IS NOT OLD.{column} AND EXISTS(SELECT 1 FROM json_each(NEW.{column}) c
            WHERE c.key IS NOT CAST(CAST(c.key AS INTEGER) AS TEXT) OR CAST(c.key AS INTEGER) NOT BETWEEN 1 AND 29
              OR c.type IS NOT 'array' OR json_array_length(c.value)<>7
              OR NOT COALESCE(typeof(c.value->>0)='integer' AND (c.value->>0)>0
                AND typeof(c.value->>2)='integer' AND (c.value->>2)>0 AND typeof(c.value->>3)='integer'
                AND EXISTS(SELECT 1 FROM enrichment_provider_config p WHERE p.metadata_provider_id=(c.value->>1))
                AND CASE c.value->>3
                    WHEN 0 THEN (c.value->>4) IS NULL AND (c.value->>5) IS NULL AND (c.value->>6) IS NULL
                    WHEN 1 THEN (c.value->>4) IS NULL AND (c.value->>5) IS NULL AND (c.value->>6) IS NULL
                    WHEN 4 THEN (c.value->>4) IS NULL AND typeof(c.value->>5) IN ('integer','real') AND (c.value->>6) IS NULL
                    WHEN 5 THEN (c.value->>4) IS NULL AND (c.value->>5) IS NULL AND EXISTS(
                        SELECT 1 FROM genre_catalog g WHERE g.key=(c.value->>6))
                    ELSE (c.value->>3) IN (2,3,6) AND typeof(c.value->>4)='text'
                        AND (c.value->>5) IS NULL AND (c.value->>6) IS NULL END,0))
        BEGIN SELECT RAISE(ABORT,'Invalid runtime choice for upload'); END''')
    for owner, key, field in (('genre_catalog', 'key', 'genre_key'),
                              ('enrichment_provider_config', 'metadata_provider_id', 'provider_id')):
        for action in ('DELETE', f'UPDATE OF {key}'):
            changed = '' if action == 'DELETE' else f'AND NEW.{key} IS NOT OLD.{key}'
            conn.execute(f'''CREATE TRIGGER retain_runtime_choice_{field}_{action.split()[0].lower()}
                BEFORE {action} ON {owner} WHEN OLD.{key} IS NOT NULL {changed}
                AND EXISTS(SELECT 1 FROM {choices.VIEW} WHERE {field}=OLD.{key})
                BEGIN SELECT RAISE(ABORT,'FOREIGN KEY constraint failed'); END''')


def validate(conn):
    names = {choices.VIEW, choices.INDEX, 'track_source_runtime_choice_refresh'}
    if set(definitions(conn, (*names, RETIRED))) != names:
        raise MigrationError('Runtime metadata choices were not moved to their uploads')
