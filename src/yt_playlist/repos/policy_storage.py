"""SQL inventory, accounting and bounded deletion of recommendation policies."""
import json

UNSERVED = ("NOT EXISTS (SELECT 1 FROM rec_served_impressions i WHERE i.model_version=p.model_version) "
            "AND NOT EXISTS (SELECT 1 FROM rec_served_impressions i "
            "WHERE CAST(json_extract(i.provenance,'$.pool_policy') AS TEXT)=p.model_version)")


def typed(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='recommendation_policies'").fetchone() is not None


def operational_protection(conn):
    """Foreign-key owners in addition to immutable impressions."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    owners = ('album_description_requests', 'group_generations', 'group_versions',
              'recommendation_generations', 'recommendation_pools', 'home_display_policies', 'model_pair_policies',
              'radio_session_policies', 'radio_seed_policies', 'recommendation_pool_revalidations')
    return ' AND '.join([
        f'NOT EXISTS(SELECT 1 FROM {table} r WHERE r.policy_version=p.version)'
        for table in owners if table in tables])


def _typed_protection(conn):
    return ('NOT EXISTS(SELECT 1 FROM recommendation_impression_policies i WHERE i.policy_version=p.version) AND '
            + operational_protection(conn))


def retained_closure(conn, before, *, protected=()):
    """SQL closure of explicit owners and policies newer than the supplied cutoff."""
    if not typed(conn):
        return None
    retained = {r[0] for r in conn.execute('SELECT version FROM recommendation_policies p '
        'WHERE created_at>=? OR NOT (' + _typed_protection(conn) + ')', (before,))}
    retained.update(protected)
    bases = set()
    for start in range(0, len(versions := sorted(retained)), 256):
        batch = versions[start:start + 256]
        bases.update(r[0] for r in conn.execute('SELECT base_version FROM recommendation_policies '
            'WHERE base_version IS NOT NULL AND version IN (' + ','.join('?' for _ in batch) + ')', batch))
    return {'recommendation_policies': retained, 'recommendation_policy_bodies': bases}


def _new_records(conn, table, key, rows):
    pending = {row[0]: row for row in rows}
    keys = list(pending)
    for start in range(0, len(keys), 256):
        batch = keys[start:start + 256]
        for row in conn.execute(f'SELECT {key} FROM {table} WHERE {key} IN (' + ','.join('?' for _ in batch) + ')', batch):
            pending.pop(row[0], None)
    return list(pending.values())


def refresh_references(conn, versions):
    """Update accounting in the same SQL transaction as impression changes."""
    if typed(conn):
        return  # Typed impression-link triggers maintain exact accounting.
    versions = list(set(versions) - {None})
    bases = set()
    for start in range(0, len(versions), 256):
        batch = versions[start:start + 256]
        rows = conn.execute('SELECT model_version,base_version FROM rec_policy_variants '
            'WHERE model_version IN (' + ','.join('?' for _ in batch) + ')', batch).fetchall()
        for version, base in rows:
            conn.execute('UPDATE rec_policy_variants SET served=('
                'EXISTS(SELECT 1 FROM rec_served_impressions WHERE model_version=?) OR '
                "EXISTS(SELECT 1 FROM rec_served_impressions WHERE CAST(json_extract(provenance,'$.pool_policy') "
                'AS TEXT)=?)) WHERE model_version=?', (version, version, version))
            bases.add(base)
    for base in bases:
        conn.execute('UPDATE rec_policy_bases SET served=EXISTS('
            'SELECT 1 FROM rec_policy_variants WHERE base_version=? AND served=1) WHERE base_version=?', (base, base))


def legacy_root_messages(conn):
    """Detach predecessor serving messages; current owners are relational."""
    from yt_playlist.repos.proposal_cache import logical_rows
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='model_pair_heads'").fetchone():
        return []
    return [row['payload'] for row in logical_rows(conn)] + [row[0] for row in conn.execute('SELECT value FROM settings')]


def history_candidates(conn, before, limit, *, versions=None):
    selected = '' if versions is None else ' AND p.version IN (' + ','.join('?' for _ in versions) + ')'
    return [dict(row) for row in conn.execute('SELECT p.* FROM recommendation_policies p WHERE created_at<? AND '
        'EXISTS(SELECT 1 FROM recommendation_impression_policies i WHERE i.policy_version=p.version) AND '
        + operational_protection(conn) + selected + ' ORDER BY created_at,version LIMIT ?',
        (before, *(versions or ()), limit))]


def retirement_pending(conn, before, *, protected):
    if not typed(conn):
        return False
    policies = conn.execute('SELECT version FROM recommendation_policies p WHERE created_at<? AND '
        + _typed_protection(conn), (before,))
    return any(row[0] not in protected for row in policies) or bool(conn.execute(
        'SELECT 1 FROM recommendation_policy_bodies b WHERE b.served=0 AND NOT EXISTS('
        'SELECT 1 FROM recommendation_policies p WHERE p.base_version=b.version) LIMIT 1').fetchone())


def retention_candidates(conn, before, *, limit, byte_limit, cursor_key, protected):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (cursor_key,)).fetchone()
    try:
        cursor = json.loads(row[0]) if row else None
        if not (isinstance(cursor, list) and len(cursor) == 2
                and isinstance(cursor[0], (float, int)) and isinstance(cursor[1], str)):
            cursor = None
    except (ValueError, TypeError):
        cursor = None
    modern = typed(conn)
    table, key, size_column = (('recommendation_policies', 'version', 'byte_size') if modern else
                               ('rec_serving_policies', 'model_version', 'length(CAST(configuration AS BLOB))'))
    after = f' AND (created_at,{key})>(?,?)' if cursor else ''
    protection = ' AND ' + _typed_protection(conn) if modern else ''
    rows, size = [], 0
    for created_at, version, byte_size in conn.execute(
            f'SELECT created_at,{key},{size_column} FROM {table} p WHERE created_at<?' + after + protection +
            f' ORDER BY created_at,{key} LIMIT ?', (before, *(cursor or ()), limit)):
        rows.append((created_at, version))
        size += byte_size
        if size >= byte_limit:
            break
    more = len(rows) == limit or size >= byte_limit
    return [row[1] for row in rows if row[1] not in protected], list(rows[-1]) if more else None


def prune(conn, versions, cursor, *, cursor_key, limit, byte_limit):
    # Recheck actual impression and owner references at the write boundary.
    modern = typed(conn)
    table, key = ('recommendation_policies', 'version') if modern else ('rec_serving_policies', 'model_version')
    protection = _typed_protection(conn) if modern else UNSERVED
    deleted = 0
    for start in range(0, len(versions), 256):
        batch = versions[start:start + 256]
        deleted += conn.execute(f'DELETE FROM {table} AS p WHERE {key} IN (' + ','.join('?' for _ in batch)
                                + ') AND ' + protection, batch).rowcount
    if cursor:
        conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (cursor_key, json.dumps(cursor)))
    else:
        conn.execute('DELETE FROM settings WHERE key=?', (cursor_key,))
    bodies, size = [], 0
    body_query = ('SELECT b.version,b.byte_size FROM recommendation_policy_bodies b WHERE b.served=0 '
                  'AND NOT EXISTS(SELECT 1 FROM recommendation_policies p WHERE p.base_version=b.version) LIMIT ?'
                  if modern else 'SELECT b.base_version,b.byte_size FROM rec_policy_bases b WHERE b.served=0 '
                  'AND NOT EXISTS(SELECT 1 FROM rec_policy_variants v WHERE v.base_version=b.base_version) LIMIT ?')
    for version, byte_size in conn.execute(body_query, (limit,)):
        bodies.append(version)
        size += byte_size
        if size >= byte_limit:
            break
    for start in range(0, len(bodies), 256):
        batch = bodies[start:start + 256]
        body_table, body_key = ('recommendation_policy_bodies', 'version') if modern else ('rec_policy_bases', 'base_version')
        conn.execute(f'DELETE FROM {body_table} WHERE {body_key} IN (' + ','.join('?' for _ in batch) + ')', batch)
    return {'policies_deleted': deleted, 'bodies_deleted': len(bodies)}
