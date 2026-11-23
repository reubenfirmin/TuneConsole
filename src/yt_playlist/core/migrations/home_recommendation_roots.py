"""Keep the exact Home recommendation roots only on their owning file manifest."""
from ..recommendation_custody import HOME_GENERATIONS, HOME_RETIREMENT_GUARD, HOME_ROOTS
from ..schema_contract import MigrationError
from ..schema_inspection import definitions
from .recommendation_custody import HOME_GUARD

NAME = 'keep_home_recommendation_roots_on_display_files'
DELETE_GUARD = 'retained_recommendation_generation_delete'
REPLACED = (HOME_GUARD, DELETE_GUARD)


def predecessor_objects(conn):
    return list(definitions(conn, REPLACED).values())


def install(conn, _context):
    for name in REPLACED:
        conn.execute('DROP TRIGGER ' + name)
    # Keep roots from other owners, including any recovered key for which the
    # matching Home manifest was already lost.
    conn.execute(f'''UPDATE recommendation_generations AS g SET evidence_roots=(
        SELECT json_group_array(json(root.value)) FROM json_each(g.evidence_roots) root
        WHERE NOT EXISTS(SELECT 1 FROM {HOME_ROOTS} AND home.value->>3=g.id
            AND root.value=json_array(home.value->>1,home.value->>2)))
        WHERE g.evidence_roots<>'[]' AND g.id IN ({HOME_GENERATIONS})''')
    conn.execute(f'''CREATE TRIGGER {HOME_RETIREMENT_GUARD}
        BEFORE UPDATE OF status ON recommendation_generations
        WHEN OLD.status='published' AND NEW.status<>'published'
            AND EXISTS({HOME_GENERATIONS} AND home.value->>3=OLD.id)
        BEGIN SELECT RAISE(ABORT,'Home display retains this reference'); END''')
    conn.execute(f'''CREATE TRIGGER {DELETE_GUARD} BEFORE DELETE ON recommendation_generations
        WHEN OLD.evidence_roots<>'[]' OR EXISTS({HOME_GENERATIONS} AND home.value->>3=OLD.id)
        BEGIN SELECT RAISE(ABORT,'Recommendation generation still has an evidence owner'); END''')


def validate(conn):
    names = {HOME_RETIREMENT_GUARD, DELETE_GUARD}
    if set(definitions(conn, (*names, HOME_GUARD))) != names:
        raise MigrationError('Incomplete Home recommendation root cutover')
