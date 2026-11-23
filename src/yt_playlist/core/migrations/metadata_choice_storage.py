"""Use the track source to identify the current explicit choice for each field."""
from ..content_work import enqueue_sql
from ..metadata_choice_selections import upgrade_selection_sql
from ..metadata_contract import ACTIVE_SQL, PREDECESSOR_ACTIVE_SQL, with_genre_keys
from ..recommendation_invalidation import REQUEST_REFRESH
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from ..source_metadata import _refresh

NAME = 'select_metadata_observations_on_track_sources'
RETIRED = 'metadata_choice_heads'
REFRESH = 'track_source_metadata_choice_refresh'
GUARD = 'track_source_metadata_choice_owner'
RETAIN = 'retained_current_metadata_choice'
INVALIDATE = 'track_source_metadata_choice_changed'


def _objects(conn):
    return conn.execute('SELECT name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL '
                        'AND (tbl_name=? OR name=? OR (type=\'trigger\' AND instr(sql,?))) ORDER BY type,name',
                        (RETIRED, 'track_sources', RETIRED)).fetchall()


def predecessor_objects(conn):
    return [sql for _, _, sql in _objects(conn)]


def install(conn, _context):
    conn.execute("ALTER TABLE track_sources ADD COLUMN choice_observations TEXT NOT NULL DEFAULT '{}' "
                 "CHECK(json_type(choice_observations)='object')")
    conn.execute('UPDATE track_sources AS s SET choice_observations='
                 '(SELECT json_group_object(h.field,h.observation_id) FROM metadata_choice_heads h WHERE h.entry_id=s.id) '
                 'WHERE id IN (SELECT entry_id FROM metadata_choice_heads)')
    for name, table, statement in _objects(conn):
        if table == RETIRED or name == 'track_sources':
            continue
        updated = upgrade_selection_sql(statement)
        if RETIRED in updated:
            raise MigrationError(f'Unknown metadata choice lookup in {name}')
        conn.execute(f'DROP TRIGGER {name}')
        conn.execute(updated)
    conn.execute(f'DROP TABLE {RETIRED}')
    source_refresh = with_genre_keys(_refresh('NEW.id').replace(PREDECESSOR_ACTIVE_SQL, ACTIVE_SQL))
    conn.execute(f'''CREATE TRIGGER {REFRESH} AFTER UPDATE OF choice_observations ON track_sources
        WHEN NEW.choice_observations IS NOT OLD.choice_observations
        BEGIN {upgrade_selection_sql(source_refresh)} END''')
    conn.execute(f'''CREATE TRIGGER {GUARD} BEFORE UPDATE OF choice_observations ON track_sources
        WHEN EXISTS(SELECT 1 FROM json_each(NEW.choice_observations) h
            WHERE h.type<>'integer' OR NOT EXISTS(SELECT 1 FROM metadata_observations o
                WHERE o.id=h.atom AND o.entry_id=NEW.id AND o.field=h.key
                AND o.origin IN ('user','legacy_choice','selection_snapshot') AND o.value_kind<>'unresolved'))
        BEGIN SELECT RAISE(ABORT,'Metadata choice has a different entry, field or origin'); END''')
    conn.execute('''CREATE TRIGGER track_source_metadata_choice_insert BEFORE INSERT ON track_sources
        WHEN NEW.choice_observations<>'{}'
        BEGIN SELECT RAISE(ABORT,'Capture the metadata choice after creating the track source'); END''')
    conn.execute(f'''CREATE TRIGGER {RETAIN} BEFORE DELETE ON metadata_observations
        WHEN EXISTS(SELECT 1 FROM track_sources s WHERE s.id=OLD.entry_id AND s.choice_observations->>OLD.field=OLD.id)
        BEGIN SELECT RAISE(ABORT,'Metadata observation is the current explicit choice'); END''')
    queue = enqueue_sql('SELECT identity_key FROM recordings WHERE id=NEW.recording_id')
    conn.execute(f'''CREATE TRIGGER {INVALIDATE} AFTER UPDATE OF choice_observations ON track_sources
        WHEN NEW.choice_observations IS NOT OLD.choice_observations AND EXISTS(
            SELECT 1 FROM (SELECT key FROM json_each(NEW.choice_observations)
                UNION SELECT key FROM json_each(OLD.choice_observations)) fields
            LEFT JOIN metadata_observations o ON o.id=(OLD.choice_observations->>fields.key)
            LEFT JOIN metadata_observations n ON n.id=(NEW.choice_observations->>fields.key)
            WHERE o.value_kind IS NOT n.value_kind OR o.text_value IS NOT n.text_value
                OR o.number_value IS NOT n.number_value)
        BEGIN {REQUEST_REFRESH}; {queue}; END''')


def validate(conn):
    names = {REFRESH, GUARD, RETAIN, INVALIDATE, 'track_source_metadata_choice_insert'}
    if set(definitions(conn, (*names, RETIRED))) != names:
        raise MigrationError('Current metadata choices were not folded into track sources')
