"""Keep ordered evidence on each conflict and Wiki cache values in settings."""
import json

from ..schema_contract import MigrationError
from ..schema_inspection import definitions


NAME = 'own_metadata_conflict_candidates_and_wiki_cache'
WIKI_CACHE_PREFIX = 'wiki_card:'
CONFLICTS = 'metadata_conflicts'
RETIRED = ('metadata_conflict_candidates', 'wiki_cards')
MARKER = 'owned_metadata_conflict_candidates_update'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute(
        'SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL AND '
        '(tbl_name IN (?,?,?) OR name=\'enrichment_conflict\') ORDER BY type,name', (CONFLICTS, *RETIRED))]


def _install_candidates(conn):
    conn.execute("ALTER TABLE metadata_conflicts ADD COLUMN candidate_positions TEXT NOT NULL DEFAULT '{}' "
                 "CHECK(json_type(candidate_positions)='object')")
    conn.execute('''UPDATE metadata_conflicts SET candidate_positions=(
        SELECT json_group_object(observation_id,position ORDER BY position,observation_id)
        FROM metadata_conflict_candidates WHERE conflict_id=metadata_conflicts.id)
        WHERE id IN (SELECT conflict_id FROM metadata_conflict_candidates)''')

    view = definitions(conn, ('enrichment_conflict',))['enrichment_conflict']
    if RETIRED[0] in view:
        view = view.replace('FROM metadata_conflict_candidates p', 'FROM json_each(c.candidate_positions) p')
        view = view.replace('o.id=p.observation_id', 'o.id=CAST(p.key AS INTEGER)')
        view = view.replace('WHERE p.conflict_id=c.id ORDER BY p.position,o.id', 'ORDER BY p.value,o.id')
        conn.execute('DROP VIEW enrichment_conflict')
        conn.execute(view)

    conn.execute('DROP TABLE metadata_conflict_candidates')
    for suffix, event in (('insert', 'INSERT'), ('update', 'UPDATE OF candidate_positions')):
        conn.execute(f'''CREATE TRIGGER owned_metadata_conflict_candidates_{suffix} BEFORE {event} ON metadata_conflicts
            WHEN EXISTS(SELECT 1 FROM json_each(NEW.candidate_positions) p
                LEFT JOIN metadata_observations o ON o.id=CAST(p.key AS INTEGER)
                WHERE p.type<>'integer' OR p.value<0 OR CAST(o.id AS TEXT) IS NOT p.key
                    OR o.field<>NEW.field OR o.value_kind='withdrawn' OR NOT (
                      (NEW.entry_id IS NOT NULL AND o.entry_id IS NEW.entry_id
                       AND (o.field_scope=NEW.field_scope OR NEW.field_scope='legacy_item'))
                      OR (NEW.recording_id IS NOT NULL AND o.recording_id IS NEW.recording_id
                          AND o.field_scope='recording')))
              OR EXISTS(SELECT 1 FROM json_each(NEW.candidate_positions) GROUP BY key HAVING count(*)>1)
            BEGIN SELECT RAISE(ABORT,'Invalid metadata conflict candidates'); END''')
    conn.execute('''CREATE TRIGGER retained_metadata_conflict_candidate BEFORE DELETE ON metadata_observations
        WHEN EXISTS(SELECT 1 FROM metadata_conflicts
                    WHERE json_extract(candidate_positions,'$."'||OLD.id||'"') IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'Observation still belongs to a metadata conflict'); END''')


def install(conn, _context):
    _install_candidates(conn)
    cards = conn.execute('SELECT * FROM wiki_cards')
    fields = tuple(column[0] for column in cards.description)
    conn.executemany('INSERT INTO settings(key,value) VALUES (?,?)',
                     ((WIKI_CACHE_PREFIX + row[0], json.dumps(dict(zip(fields, row)))) for row in cards))
    conn.execute('DROP TABLE wiki_cards')


def validate(conn):
    if set(definitions(conn, (MARKER, *RETIRED))) != {MARKER}:
        raise MigrationError('Retired metadata conflict or Wiki storage remains')
