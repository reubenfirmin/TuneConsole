"""Keep selected learned reports and captured context mappings under exact keys."""
import json

from .. import choice_learning_state, mix_choices, worker_settings
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .account_storage import _sequence

NAME = 'keep_learned_choices_in_settings'
RETIRED = ('choice_learning_publications', 'choice_context_projections')
MARKER = 'choice_learned_descriptor_insert'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        "AND (tbl_name IN (?,?) OR name='mix_descriptor_retained') ORDER BY type,name", RETIRED)]


def _descriptor_guards(conn):
    conn.execute('DROP TRIGGER mix_descriptor_retained')
    conn.execute(f'''CREATE TRIGGER mix_descriptor_retained BEFORE DELETE ON mix_choice_descriptors
        WHEN EXISTS(SELECT 1 FROM mix_choice_menus m,json_each(m.options) o WHERE o.value->>2=OLD.id)
          OR OLD.id IN ({choice_learning_state.DESCRIPTORS})
        BEGIN SELECT RAISE(ABORT,'A menu or learner retains this descriptor'); END''')
    for verb in ('INSERT', 'UPDATE'):
        name = 'choice_learned_descriptor_' + verb.lower()
        conn.execute(f'''CREATE TRIGGER {name} BEFORE {verb} ON settings
            WHEN NEW.key IN {choice_learning_state.KEYS} AND EXISTS(
                SELECT 1 FROM json_each(NEW.value,'$.document.contexts') c
                WHERE c.value->>'$.descriptor_id' IS NOT NULL AND NOT EXISTS(
                    SELECT 1 FROM mix_choice_descriptors d WHERE d.id=c.value->>'$.descriptor_id'))
            BEGIN SELECT RAISE(ABORT,'Unknown captured mix descriptor'); END''')


def install(conn, _context):
    for identity, surface, at, document in conn.execute(
            'SELECT id,surface,built_at,document FROM choice_learning_publications'):
        choice_learning_state.save(conn, surface, identity, at, document)
    worker_settings.save(conn, choice_learning_state.ID, str(_sequence(conn, 'choice_learning_publications') or 0))
    for source, kind, context, digest in conn.execute('SELECT * FROM choice_context_projections'):
        worker_settings.save(conn, mix_choices.CONTEXT_PREFIX + kind + ':' + source,
                             json.dumps({'context': context, 'source_digest': digest.hex()}))
    _descriptor_guards(conn)
    for table in RETIRED:
        conn.execute('DROP TABLE ' + table)


def validate(conn):
    if set(definitions(conn, (*RETIRED, MARKER))) != {MARKER}:
        raise MigrationError('Incomplete learned choice settings cutover')
