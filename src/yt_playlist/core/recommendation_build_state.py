"""Pool IDs staged by the recommendation generation that owns the build."""

STAGED_INDEX = 'ix_recommendation_generations_staged_pools'


def builds_with_pool(pool_id):
    return (f'SELECT build.id FROM recommendation_generations build INDEXED BY {STAGED_INDEX},'
            f"json_each(build.staged_pool_ids) claim WHERE build.staged_pool_ids<>'[]' AND claim.value={pool_id}")


def stage_pool(conn, generation, pool_id):
    conn.execute("UPDATE recommendation_generations SET staged_pool_ids=json_insert(staged_pool_ids,'$[#]',?) "
                 'WHERE id=? AND NOT EXISTS(SELECT 1 FROM json_each(staged_pool_ids) WHERE value=?)',
                 (pool_id, generation, pool_id))


def release_pool(conn, generation, pool_id):
    path = conn.execute('SELECT j.fullkey FROM recommendation_generations g,json_each(g.staged_pool_ids) j '
                        'WHERE g.id=? AND j.value=?', (generation, pool_id)).fetchone()
    if path:
        conn.execute('UPDATE recommendation_generations SET staged_pool_ids=json_remove(staged_pool_ids,?) WHERE id=?',
                     (path[0], generation))
