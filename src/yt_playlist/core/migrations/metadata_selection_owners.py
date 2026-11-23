"""Move each selected provider or observation to its metadata subject."""
import re

from .. import metadata_selections as selections, worker_settings
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .runner import PreparationChanged

NAME = 'own_current_metadata_selections_on_subjects'
RETIRED = 'recording_metadata_selections'
OWNERS = selections.OWNERS
SCOPES = ('recording', 'entry')
VIEWS = ('recording_field_sources', 'item_field_sources')
MARKER = 'metadata_selections_valid_recordings'
OLD_GUARDS = tuple(f'owned_{scope}_metadata_selections_{action}' for scope in SCOPES for action in ('insert', 'update'))
PAGE_SIZE = 128


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND '
        '(tbl_name=? OR name IN (?,?) OR (type IN (\'trigger\',\'view\') AND instr(sql,?))) ORDER BY type,name',
        (RETIRED, *OWNERS.values(), RETIRED))]


def _cursor(scope):
    return f'prepare_metadata_selections_{scope}_after_id'


def preparing(conn):
    return worker_settings.read(conn, _cursor('recording')) is not None


def _document():
    return '''json_group_object(field,json_array(observation_id,attribution_status,legacy_source,
        json(CASE WHEN selected_at IS NOT NULL THEN printf('%!.26g',selected_at) END),runtime_provider_id))'''


def _remaining(scope, after):
    subject = f'{scope}_id'
    return (f'{subject}>?', (int(after),)) if after else (f'{subject} IS NOT NULL', ())


def _copy_page(conn, scope, owner):
    after = worker_settings.read(conn, _cursor(scope))
    where, args = _remaining(scope, after)
    subject = f'{scope}_id'
    boundary = conn.execute(f'''SELECT max(subject) FROM (SELECT {subject} AS subject FROM {RETIRED}
        WHERE {where} GROUP BY {subject} ORDER BY {subject} LIMIT ?)''', (*args, PAGE_SIZE)).fetchone()[0]
    if boundary is None:
        return False
    conn.execute(f'''WITH attributed AS (SELECT {subject} AS subject,{_document()} AS metadata FROM {RETIRED}
        WHERE {where} AND {subject}<=? GROUP BY {subject}) UPDATE {owner} SET {selections.COLUMN}=attributed.metadata
        FROM attributed WHERE {owner}.id=attributed.subject''', (*args, boundary))
    worker_settings.save(conn, _cursor(scope), str(boundary))
    return True


def prepare(conn, version):
    while True:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM schema_changes WHERE version=?', (version,)).fetchone():
                return
            if not preparing(conn):
                column = selections.COLUMN
                for scope, owner in OWNERS.items():
                    conn.execute(f"ALTER TABLE {owner} ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}' "
                                 f"CHECK(json_type({column})='object')")
                    worker_settings.save(conn, _cursor(scope), '')
            elif not any(_copy_page(conn, scope, owner) for scope, owner in OWNERS.items()):
                return


def _owner_guards(conn, scope, owner):
    column = selections.COLUMN
    observation = "choice.value->>'$[0]'"
    status = "choice.value->>'$[1]'"
    source = "choice.value->>'$[2]'"
    at = "choice.value->>'$[3]'"
    provider = "choice.value->>'$[4]'"
    scope_check = "o.field_scope='recording'" if scope == 'recording' else "o.field_scope IN ('item','release')"
    conn.execute(f'''CREATE TRIGGER metadata_selections_valid_{owner} BEFORE UPDATE OF {column} ON {owner}
        WHEN NEW.{column} IS NOT OLD.{column} AND EXISTS(SELECT 1 FROM json_each(NEW.{column}) choice
            WHERE choice.key NOT IN ({selections.fields_sql(scope)}) OR choice.type IS NOT 'array'
                OR json_array_length(choice.value)<>5
                OR NOT COALESCE(({status} IS NULL AND {observation} IS NULL AND {source} IS NULL
                    AND {at} IS NULL AND {provider} IS NOT NULL)
                 OR ({status}='legacy_unresolved' AND {observation} IS NULL AND typeof({source})='text')
                 OR ({status}='evidenced' AND {source} IS NULL AND EXISTS(SELECT 1 FROM metadata_observations o
                    WHERE o.id=({observation}) AND o.{scope}_id=NEW.id AND o.field=choice.key AND {scope_check}
                      AND o.origin NOT IN ('user','legacy_choice') AND o.value_kind IN ('value','null','blank'))),0)
                OR ({at} IS NOT NULL AND (typeof({at}) NOT IN ('real','integer') OR abs({at})>1.7976931348623157e308))
                OR ({provider} IS NOT NULL AND NOT EXISTS(SELECT 1 FROM enrichment_provider_config p
                    WHERE p.metadata_provider_id=({provider}))))
        BEGIN SELECT RAISE(ABORT,'Invalid metadata selection for this subject'); END''')
    conn.execute(f'''CREATE TRIGGER metadata_selections_insert_{owner} BEFORE INSERT ON {owner}
        WHEN NEW.{column}<>'{{}}' BEGIN SELECT RAISE(ABORT,'Select metadata after admitting its subject'); END''')
    conn.execute(f'''CREATE TRIGGER metadata_selections_keep_{owner} BEFORE DELETE ON {owner}
        WHEN EXISTS(SELECT 1 FROM json_each(OLD.{column}) choice WHERE {status} IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'FOREIGN KEY constraint failed'); END''')


def install(conn, _context):
    for scope in SCOPES:
        after = worker_settings.read(conn, _cursor(scope))
        where, args = _remaining(scope, after)
        if after is None or conn.execute(f'SELECT 1 FROM {RETIRED} WHERE {where} LIMIT 1', args).fetchone():
            raise PreparationChanged('Metadata selection preparation is incomplete')
        worker_settings.save(conn, _cursor(scope), None)
    objects = definitions(conn, (*VIEWS, *(f'remove_{scope}_id_runtime_selection' for scope in SCOPES)))
    for name in VIEWS:
        conn.execute(f'DROP VIEW {name}')
    for scope, owner in OWNERS.items():
        cleanup = f'remove_{scope}_id_runtime_selection'
        conn.execute(f'DROP TRIGGER {cleanup}')
        conn.execute(re.sub(r'\s+DELETE FROM ' + RETIRED + r'[^;]*;', '', objects[cleanup]))
        _owner_guards(conn, scope, owner)
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute(selections.read_view())
    for scope, name in zip(SCOPES, VIEWS):
        conn.execute(objects[name].replace(RETIRED, '(' + selections.projection(scope) + ')'))
    conn.execute(f'''CREATE TRIGGER retained_metadata_selection_observation BEFORE DELETE ON metadata_observations
        WHEN EXISTS(SELECT 1 FROM recordings WHERE id=OLD.recording_id
            AND ({selections.COLUMN}->OLD.field)->>0=OLD.id)
          OR EXISTS(SELECT 1 FROM track_sources WHERE id=OLD.entry_id
            AND ({selections.COLUMN}->OLD.field)->>0=OLD.id)
        BEGIN SELECT RAISE(ABORT,'FOREIGN KEY constraint failed'); END''')
    conn.execute(f'''CREATE TRIGGER retained_metadata_selection_provider BEFORE DELETE ON enrichment_provider_config
        WHEN OLD.metadata_provider_id IS NOT NULL AND EXISTS(SELECT 1 FROM {selections.VIEW}
            WHERE runtime_provider_id=OLD.metadata_provider_id)
        BEGIN SELECT RAISE(ABORT,'FOREIGN KEY constraint failed'); END''')


def validate(conn):
    names = {MARKER, selections.VIEW, 'retained_metadata_selection_observation', 'retained_metadata_selection_provider'}
    if set(definitions(conn, (*names, RETIRED))) != names:
        raise MigrationError('Metadata selections were not moved to their subjects')
