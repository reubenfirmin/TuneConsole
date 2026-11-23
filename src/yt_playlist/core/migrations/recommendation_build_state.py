"""Move outstanding pool claims onto their recommendation generation."""

from ..recommendation_build_state import STAGED_INDEX, builds_with_pool
from ..schema_contract import MigrationError
from ..schema_inspection import definitions

NAME = 'keep_staged_pools_on_recommendation_generations'
RETIRED = 'recommendation_build_pools'
CLAIM_GUARDS = ('active_recommendation_build_pool', 'live_recommendation_build_pool',
                'private_recommendation_build_pool')
REPLACEMENTS = {
    'SELECT 1 FROM recommendation_build_pools b WHERE b.pool_id=p.id': builds_with_pool('p.id'),
    'SELECT 1 FROM recommendation_build_pools WHERE pool_id=NEW.id': builds_with_pool('NEW.id'),
    'EXISTS(SELECT 1 FROM recommendation_build_pools WHERE generation_id=OLD.id)': "NEW.staged_pool_ids<>'[]'",
}


def _statements(conn):
    return conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE sql IS NOT NULL "
                        "AND (tbl_name=? OR name='recommendation_generations' "
                        "OR (type='trigger' AND instr(sql,?))) ORDER BY type,name", (RETIRED, RETIRED)).fetchall()


def predecessor_objects(conn):
    return [sql for _, _, _, sql in _statements(conn)]


def install(conn, _context):
    conn.execute("ALTER TABLE recommendation_generations ADD COLUMN staged_pool_ids TEXT NOT NULL DEFAULT '[]' "
                 "CHECK(json_type(staged_pool_ids)='array')")
    conn.execute('UPDATE recommendation_generations AS g SET staged_pool_ids=(SELECT json_group_array(pool_id) '
                 'FROM (SELECT pool_id FROM recommendation_build_pools WHERE generation_id=g.id ORDER BY pool_id)) '
                 'WHERE EXISTS(SELECT 1 FROM recommendation_build_pools WHERE generation_id=g.id)')
    conn.execute(f"CREATE INDEX {STAGED_INDEX} ON recommendation_generations(id) WHERE staged_pool_ids<>'[]'")
    for kind, name, owner, sql in _statements(conn):
        if kind != 'trigger' or owner == RETIRED:
            continue
        updated = sql
        for source, target in REPLACEMENTS.items():
            updated = updated.replace(source, target)
        if RETIRED in updated:
            raise MigrationError(f'Unknown staged pool dependency in {name}')
        conn.execute(f'DROP TRIGGER {name}')
        conn.execute(updated)
    conn.execute(f'DROP TABLE {RETIRED}')
    conn.execute(f'''CREATE TRIGGER validate_recommendation_staged_pools
        BEFORE UPDATE OF staged_pool_ids ON recommendation_generations
        WHEN NEW.staged_pool_ids<>OLD.staged_pool_ids
        BEGIN
            SELECT RAISE(ABORT,'Recommendation pool requires an active build')
            WHERE NEW.staged_pool_ids<>'[]' AND (NEW.build_owner IS NULL OR NEW.status='published');
            SELECT RAISE(ABORT,'Invalid staged recommendation pool')
            FROM json_each(NEW.staged_pool_ids) AS proposed
            LEFT JOIN recommendation_pools p ON p.id=proposed.value
            WHERE proposed.type<>'integer' OR p.id IS NULL OR p.retiring=1;
            SELECT RAISE(ABORT,'Recommendation pool is leased by another build')
            FROM json_each(NEW.staged_pool_ids) AS proposed
            JOIN recommendation_pools p ON p.id=proposed.value
            WHERE p.complete=0 AND EXISTS({builds_with_pool('proposed.value')} AND build.id<>NEW.id);
        END''')
    conn.execute(f'''CREATE TRIGGER staged_recommendation_pool_delete BEFORE DELETE ON recommendation_pools
        WHEN EXISTS({builds_with_pool('OLD.id')})
        BEGIN SELECT RAISE(ABORT,'Recommendation pool still has a build owner'); END''')


def validate(conn):
    names = {STAGED_INDEX, 'validate_recommendation_staged_pools', 'staged_recommendation_pool_delete'}
    if set(definitions(conn, (*names, RETIRED, *CLAIM_GUARDS))) != names:
        raise MigrationError('Staged recommendation pool claims were not folded into the builds')
