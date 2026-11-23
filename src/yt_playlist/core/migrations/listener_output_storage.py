"""Keep the current listener outputs and their artifact references in settings."""
import json

from .. import library_taste_summary, listener_state, model_state, recommendation_outputs
from ..schema_contract import MigrationError, tokens
from ..schema_inspection import definitions


NAME = 'fold_listener_outputs_and_model_references'
RETIRED = ('library_taste_summary', 'listener_model_documents', 'taste_model_state', 'model_autotune_output')


def predecessor_objects(conn):
    marks = ','.join('?' for _ in RETIRED)
    return [sql for sql, in conn.execute(
        f'SELECT sql FROM main.sqlite_schema WHERE tbl_name IN ({marks}) AND sql IS NOT NULL ORDER BY name',
        RETIRED)]


def _ownership_triggers():
    keys = tuple(model_state.KEYS.values())
    old = f"CASE WHEN OLD.key IN {keys} THEN json_extract(OLD.value,'$.recovery_filename') END"
    new = f"CASE WHEN NEW.key IN {keys} THEN json_extract(NEW.value,'$.recovery_filename') END"
    for event, condition in (
        ('INSERT', f'({new}) IS NOT NULL'),
        ('DELETE', f'({old}) IS NOT NULL'),
        ('UPDATE OF key,value', f'({old}) IS NOT ({new}) OR (OLD.key IS NOT NEW.key AND ({old}) IS NOT NULL)'),
    ):
        name = 'collection_recovery_changed_model_settings_' + event.split()[0].lower()
        yield name, (f'CREATE TRIGGER {name} AFTER {event} ON settings WHEN {condition} BEGIN '
                     'UPDATE collection_file_writes SET ownership_revision=ownership_revision+1 WHERE id=0; END')


def install(conn, _context):
    state = {}
    summary = conn.execute('SELECT payload,built_at FROM library_taste_summary').fetchone()
    if summary is not None:
        state[library_taste_summary.KEY] = {'payload': summary[0], 'built_at': summary[1]}
    for surface, payload, updated_at in conn.execute('SELECT surface,payload,updated_at FROM listener_model_documents'):
        state[surface] = {'payload': payload, 'updated_at': updated_at}
    model_fields = ('revision', 'generation', 'input_fingerprint', 'genre_revision', 'fit_fingerprint',
                    *model_state.REFERENCE_FIELDS)
    for identity, *fields in conn.execute(f'SELECT id,{",".join(model_fields)} FROM taste_model_state'):
        state[model_state.KEYS[identity]] = dict(zip(model_fields, fields, strict=True))
    autotune_fields = ('observed_at', 'filename', 'content_digest', 'byte_size')
    autotune = conn.execute(f'SELECT {",".join(autotune_fields)} FROM model_autotune_output').fetchone()
    if autotune is not None:
        state[recommendation_outputs.AUTOTUNE_SETTING] = dict(zip(autotune_fields, autotune, strict=True))

    conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)',
                     ((key, json.dumps(value, allow_nan=False)) for key, value in state.items()))
    for table in RETIRED:
        conn.execute(f'DROP TABLE {table}')
    for _, sql in _ownership_triggers():
        conn.execute(sql)


def validate(conn):
    guards = dict(_ownership_triggers())
    schema = definitions(conn, (*RETIRED, *guards))
    if set(RETIRED).intersection(schema) or any(tokens(schema.get(name)) != tokens(sql) for name, sql in guards.items()):
        raise MigrationError('Incomplete listener output and model reference storage')
