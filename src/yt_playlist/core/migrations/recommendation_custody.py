"""Retain exact evidence owners on the published recommendation generation."""

from ..publication_files import HOME
from ..recommendation_custody import HOME_RETIREMENT_GUARD, INDEX, remove
from ..schema_contract import MigrationError
from ..schema_inspection import definitions

NAME = 'keep_recommendation_evidence_roots_on_generations'
RETIRED = 'recommendation_generation_refs'
TRANSITION = 'recommendation_generation_transition'
PROPOSAL = 'retired_proposal_recommendation_references'
HOME_GUARD = 'home_retains_recommendation_generation_refs'


def predecessor_objects(conn):
    return [sql for sql, in conn.execute('SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL '
        'AND (tbl_name=? OR name IN (?,?,?)) ORDER BY type,name',
        (RETIRED, 'recommendation_generations', TRANSITION, PROPOSAL))]


def install(conn, _context):
    conn.execute("ALTER TABLE recommendation_generations ADD COLUMN evidence_roots TEXT NOT NULL DEFAULT '[]' "
                 "CHECK(json_type(evidence_roots)='array' AND (evidence_roots='[]' OR status='published'))")
    conn.execute('UPDATE recommendation_generations AS g SET evidence_roots='
                 '(SELECT json_group_array(json_array(kind,external_key)) FROM '
                 '(SELECT kind,external_key FROM recommendation_generation_refs WHERE generation_id=g.id '
                 'ORDER BY kind,external_key)) WHERE id IN (SELECT generation_id FROM recommendation_generation_refs)')
    conn.execute(f"CREATE INDEX {INDEX} ON recommendation_generations(id) WHERE evidence_roots<>'[]'")
    transition = definitions(conn, (TRANSITION,))[TRANSITION]
    conn.execute(f'DROP TRIGGER {TRANSITION}')
    replaced = transition.replace('NOT EXISTS(SELECT 1 FROM recommendation_generation_refs WHERE generation_id=OLD.id)',
                                  "OLD.evidence_roots='[]'")
    if RETIRED in replaced:
        raise MigrationError('Unknown recommendation generation transition guard')
    conn.execute(replaced)
    conn.execute(f'DROP TRIGGER {PROPOSAL}')
    conn.execute(f'DROP TABLE {RETIRED}')
    keys = ','.join("'proposal:'||OLD.surface||':" + field + "'" for field in
                    ('context', 'choice_context', 'album_choice_context', 'recommendations'))
    prune = remove(f"root.value->>0 IN ('prepared','comparison') AND root.value->>1 IN ({keys})")
    conn.execute(f'CREATE TRIGGER {PROPOSAL} BEFORE DELETE ON rec_proposals BEGIN {prune}; END')
    conn.execute(f'''CREATE TRIGGER {HOME_GUARD} BEFORE UPDATE OF evidence_roots ON recommendation_generations
        WHEN OLD.evidence_roots<>NEW.evidence_roots AND EXISTS(
            SELECT 1 FROM settings p,json_each(p.value,'$.recommendation_roots') r
            WHERE p.key GLOB '{HOME}*' AND r.value->>3=OLD.id
            AND EXISTS(SELECT 1 FROM json_each(OLD.evidence_roots) o WHERE o.value=json_array(r.value->>1,r.value->>2))
            AND NOT EXISTS(SELECT 1 FROM json_each(NEW.evidence_roots) n WHERE n.value=json_array(r.value->>1,r.value->>2)))
        BEGIN SELECT RAISE(ABORT,'Home display retains this reference'); END''')
    conn.execute('''CREATE TRIGGER retained_recommendation_generation_delete BEFORE DELETE ON recommendation_generations
        WHEN OLD.evidence_roots<>'[]'
        BEGIN SELECT RAISE(ABORT,'Recommendation generation still has an evidence owner'); END''')


def validate(conn):
    home = HOME_RETIREMENT_GUARD if definitions(conn, (HOME_RETIREMENT_GUARD,)) else HOME_GUARD
    names = {INDEX, TRANSITION, PROPOSAL, home, 'retained_recommendation_generation_delete'}
    if set(definitions(conn, (*names, RETIRED))) != names:
        raise MigrationError('Recommendation evidence roots were not folded into the published generation')
