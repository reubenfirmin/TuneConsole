"""SQLite storage for shared policies; callers own transactions and serving locks."""
import json
from itertools import chain

from yt_playlist.core.policy_config import BASE_FIELD
from yt_playlist.core.db_space import require_database_space
from yt_playlist.core import policy_documents
from yt_playlist.core.evidence_ledger import canonical

# Bound disposable preparation independently of the 90-day served ledger. A full
# hour protects ordinary readers; pinned offer readers can restore an older body.
GRACE_SECONDS = 3600
SCAN_LIMIT = 4096
SCAN_BYTES = 8 * 1024 * 1024
MAX_UNUSED_BYTES = 64 * 1024 * 1024
MAX_UNUSED_VARIANTS = 131072
MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_BATCH_VARIANTS = 16384
CURSOR = 'rec_unused_policy_cursor'

UNSERVED = ("NOT EXISTS (SELECT 1 FROM rec_served_impressions i WHERE i.model_version=p.model_version) "
            "AND NOT EXISTS (SELECT 1 FROM rec_served_impressions i "
            "WHERE CAST(json_extract(i.provenance,'$.pool_policy') AS TEXT)=p.model_version)")


class PolicyStorageFull(RuntimeError):
    pass


def typed(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='recommendation_policies'").fetchone() is not None


def operational_protection(conn):
    """The concrete policy owners, in addition to immutable impressions."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    owners = ('album_description_requests', 'group_generations', 'group_versions',
              'recommendation_generations', 'recommendation_pools', 'home_display_policies', 'model_pair_policies',
              'radio_session_policies', 'radio_seed_policies')
    return ' AND '.join([
        f'NOT EXISTS(SELECT 1 FROM {table} r WHERE r.policy_version=p.version)'
        for table in owners if table in tables])


def _typed_protection(conn):
    return ('NOT EXISTS(SELECT 1 FROM recommendation_impression_policies i WHERE i.policy_version=p.version) AND '
            +operational_protection(conn))


def retained_closure(conn, now):
    """Direct rebuild disposition using the same installed owner checks as GC."""
    if not typed(conn):
        return None
    retained = {r[0] for r in conn.execute('SELECT version FROM recommendation_policies p '
        'WHERE created_at>=? OR NOT (' + _typed_protection(conn) + ')', (now-GRACE_SECONDS,))}
    retained.update(protected_references(conn))
    bases = set()
    for start in range(0, len(versions := sorted(retained)), 256):
        batch = versions[start:start+256]
        bases.update(r[0] for r in conn.execute('SELECT base_version FROM recommendation_policies '
            'WHERE base_version IS NOT NULL AND version IN ('+','.join('?' for _ in batch)+')',batch))
    return {'recommendation_policies': retained, 'recommendation_policy_bodies': bases}


def _publish_typed(conn, rows, bases, enforce_budget):
    if rows or bases:
        require_database_space(conn, 2 * (sum(len(row[1].encode()) for row in rows) + sum(row[2] for row in bases)))
    # Serialize the budget decision with installation, including callers which
    # have not yet made their first SQL mutation in a surrounding context.
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    if enforce_budget:
        batch_size = sum(len(row[1].encode()) for row in rows) + sum(row[2] for row in bases)
        if batch_size > MAX_BATCH_BYTES or max(len(rows), len(bases)) > MAX_BATCH_VARIANTS:
            raise PolicyStorageFull('Prepared policy batch exceeds its storage budget; retaining published pools')
        new_rows = _new_records(conn, 'recommendation_policies', 'version', rows)
        new_bases = _new_records(conn, 'recommendation_policy_bodies', 'version', bases)
        # Account for the actual canonical file records, including their format
        # wrapper. Raw compact messages undercount small variants significantly.
        size = sum(len(canonical({'format': policy_documents.FORMAT, 'configuration': json.loads(row[1])}))
                   for row in chain(new_rows, new_bases))
        count, variants = conn.execute('SELECT count(*),COALESCE(sum(byte_size),0) '
                                      'FROM recommendation_policies WHERE served=0').fetchone()
        bodies = conn.execute('SELECT COALESCE(sum(byte_size),0) FROM recommendation_policy_bodies '
                              'WHERE served=0').fetchone()[0]
        if count + len(new_rows) > MAX_UNUSED_VARIANTS or bodies + variants + size > MAX_UNUSED_BYTES:
            raise PolicyStorageFull('Unused recommendation policies reached their storage budget; retaining published pools')
    # Match the original storage boundary: publication opens a transaction when
    # necessary, while its caller owns commit/rollback with the associated work.
    policy_documents.install(conn, rows, bases)


def _new_records(conn, table, key, rows):
    pending = {row[0]: row for row in rows}
    keys = list(pending)
    for start in range(0, len(keys), 256):
        batch = keys[start:start + 256]
        marks = ','.join('?' for _ in batch)
        for row in conn.execute(f'SELECT {key} FROM {table} WHERE {key} IN ({marks})', batch):
            pending.pop(row[0], None)
    return list(pending.values())


def refresh_references(conn, versions):
    """Update bounded accounting with the same transaction as impression changes.

    Partial indexes keep quota checks independent of the retained served history.
    Deletion still checks actual references rather than trusting accounting flags.
    """
    if typed(conn):
        return  # The typed impression link triggers maintain exact accounting.
    versions = list(set(versions) - {None})
    bases = set()
    for start in range(0, len(versions), 256):
        batch = versions[start:start + 256]
        marks = ','.join('?' for _ in batch)
        rows = conn.execute(f'SELECT model_version,base_version FROM rec_policy_variants '
                            f'WHERE model_version IN ({marks})', batch).fetchall()
        for version, base in rows:
            conn.execute('UPDATE rec_policy_variants SET served=('
                'EXISTS(SELECT 1 FROM rec_served_impressions WHERE model_version=?) OR '
                "EXISTS(SELECT 1 FROM rec_served_impressions WHERE CAST(json_extract(provenance,'$.pool_policy') "
                'AS TEXT)=?)) WHERE model_version=?', (version, version, version))
            bases.add(base)
    for base in bases:
        conn.execute('UPDATE rec_policy_bases SET served=EXISTS('
            'SELECT 1 FROM rec_policy_variants WHERE base_version=? AND served=1) WHERE base_version=?', (base, base))


def publish(conn, rows, bases=(), *, enforce_budget=False):
    rows, bases = list(rows), list(bases)
    if typed(conn):
        return _publish_typed(conn, rows, bases, enforce_budget)
    if rows or bases:
        require_database_space(conn, 2 * (sum(len(row[1].encode()) for row in rows) + sum(row[2] for row in bases)))
    if enforce_budget:
        batch_size = sum(len(row[1].encode()) for row in rows) + sum(row[2] for row in bases)
        if batch_size > MAX_BATCH_BYTES or len(rows) > MAX_BATCH_VARIANTS:
            raise PolicyStorageFull('Prepared policy batch exceeds its storage budget; retaining published pools')
        new_rows = _new_records(conn, 'rec_serving_policies', 'model_version', rows)
        new_bases = _new_records(conn, 'rec_policy_bases', 'base_version', bases)
        size = sum(len(row[1].encode()) for row in new_rows) + sum(row[2] for row in new_bases)
        unused, variant_bytes = conn.execute(
            'SELECT count(*),COALESCE(sum(length(CAST(p.configuration AS BLOB))),0) '
            'FROM rec_policy_variants v JOIN rec_serving_policies p USING(model_version) '
            'WHERE v.served=0').fetchone()
        body_bytes = conn.execute(
            'SELECT COALESCE(sum(byte_size),0) FROM rec_policy_bases WHERE served=0').fetchone()[0]
        if unused + len(new_rows) > MAX_UNUSED_VARIANTS or body_bytes + variant_bytes + size > MAX_UNUSED_BYTES:
            raise PolicyStorageFull(f'Unused recommendation policies reached their storage budget: '
                f'{unused} variants, {body_bytes + variant_bytes} bytes; '
                f'batch adds {len(new_rows)} variants and {size} bytes. '
                'Retaining published pools until maintenance frees space')
    conn.executemany('INSERT OR IGNORE INTO rec_policy_bases(base_version,configuration,byte_size,created_at) '
                     'VALUES (?,?,?,?)', bases)
    conn.executemany('INSERT OR IGNORE INTO rec_serving_policies VALUES (?,?,?)', rows)
    links = [(version, value[BASE_FIELD]) for version, raw, _ in rows
             if BASE_FIELD in (value := json.loads(raw))]
    conn.executemany('INSERT OR IGNORE INTO rec_policy_variants(model_version,base_version) VALUES (?,?)', links)
    refresh_references(conn, (version for version, _ in links))


def configuration(conn, version):
    if typed(conn):
        value = policy_documents.configuration(conn, version)
        if value is None:
            from yt_playlist.core import policy_history
            if policy_history.available(conn):
                return policy_history.configuration(conn,version)
        return value
    row = conn.execute('SELECT configuration FROM rec_serving_policies WHERE model_version=?',
                       (version,)).fetchone()
    if row is None:
        return None
    result = json.loads(row[0])
    base = result.pop(BASE_FIELD, None)
    if base is not None:
        body = conn.execute('SELECT configuration FROM rec_policy_bases WHERE base_version=?', (base,)).fetchone()
        if body is None:
            raise ValueError(f'Missing shared recommendation policy body: {base}')
        result = {**json.loads(body[0]), **result}
    return result


def captured(conn, versions):
    """Detach policies from a pinned offer reader, including their shared bodies.

    An old prepared pool may disappear while its queued offer is being measured.
    Republish its immutable policy with the impression in a single transaction.
    """
    if typed(conn):
        return policy_documents.captured(conn, versions)
    rows, bases = [], {}
    for version in set(versions) - {None}:
        row = conn.execute('SELECT * FROM rec_serving_policies WHERE model_version=?', (version,)).fetchone()
        if row is None:
            continue
        rows.append(tuple(row))
        base = json.loads(row['configuration']).get(BASE_FIELD)
        if base and base not in bases:
            body = conn.execute('SELECT base_version,configuration,byte_size,created_at FROM rec_policy_bases '
                                'WHERE base_version=?', (base,)).fetchone()
            if body is None:
                raise ValueError(f'Missing captured policy body: {base}')
            bases[base] = tuple(body)
    return rows, list(bases.values())


def references(value):
    """Finite legacy serving-message paths, used only before owner conversion.

    Arbitrary keys and sixteen-character strings are not policy evidence.
    Current publications retain relational roots instead of scanning messages.
    """
    from yt_playlist.core.home_outputs import policy_references
    def capture(item):
        if not isinstance(item, dict):
            return
        for field in ('model_version', 'pool_policy'):
            version = item.get(field)
            if type(version) in (str, int) and str(version):
                result.add(str(version))
    def track(item):
        capture(item)
        if isinstance(item, dict):
            capture(item.get('serving_context'))
            capture(item.get('provenance'))
    result = set()
    if isinstance(value, list):
        for item in value:
            track(item)
        yield from sorted(result)
        return
    if not isinstance(value, dict):
        return
    documents = [value]
    if isinstance(value.get('data'), dict):
        documents.append(value['data'])
    for document in documents:
        result.update(policy_references(document))
        capture(document)
        if type(document.get('policy_version')) is str and document['policy_version']:
            result.add(document['policy_version'])
        for item in document.get('policies', []):
            capture(item)
        bundle = document.get('bundles', document)
        if not isinstance(bundle, dict):
            continue
        for mode, lanes in bundle.items():
            if not mode.startswith('_') and isinstance(lanes, dict):
                for pool in lanes.values():
                    if isinstance(pool, list):
                        for item in pool:
                            track(item)
        vibes = bundle.get('_vibes', {})
        if isinstance(vibes, dict):
            for lanes in vibes.get('pools', {}).values():
                pools = [lanes] if isinstance(lanes, list) else lanes.values() if isinstance(lanes, dict) else []
                for pool in pools:
                    if isinstance(pool, list):
                        for item in pool:
                            track(item)
            for lanes in vibes.get('genres', {}).values():
                if isinstance(lanes, dict):
                    for genres in lanes.values():
                        if isinstance(genres, dict):
                            for pool in genres.values():
                                if isinstance(pool, list):
                                    for item in pool:
                                        track(item)
    yield from sorted(result)


def protected_references(conn):
    from yt_playlist.repos.proposal_cache import logical_rows

    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='model_pair_heads'").fetchone():
        # All current serving roots are relational. Old schemas alone need the
        # compatibility inventory while their one-time conversion is pending.
        return set()
    protected = set()
    values = (row['payload'] for row in logical_rows(conn))
    for raw in chain(values, (row[0] for row in conn.execute('SELECT value FROM settings'))):
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            value = raw
        protected.update(references(value))
    return protected


def retention_candidates(conn, now, *, limit=None, cursor_key=CURSOR):
    limit = SCAN_LIMIT if limit is None else limit
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
            f' ORDER BY created_at,{key} LIMIT ?', (now - GRACE_SECONDS, *(cursor or ()), limit)):
        rows.append((created_at, version))
        size += byte_size
        if size >= SCAN_BYTES:
            break
    protected = protected_references(conn) if rows else set()
    more = len(rows) == limit or size >= SCAN_BYTES
    return [row[1] for row in rows if row[1] not in protected], list(rows[-1]) if more else None


def prune(conn, versions, cursor, *, cursor_key=CURSOR):
    # Recheck both impression paths at the write boundary, after any concurrent
    # offer was inserted. Prepared/settings roots are guarded by snapshot.publish.
    modern = typed(conn)
    table, key = ('recommendation_policies', 'version') if modern else ('rec_serving_policies', 'model_version')
    protection = _typed_protection(conn) if modern else UNSERVED
    deleted = 0
    for start in range(0, len(versions), 256):
        batch = versions[start:start + 256]
        marks = ','.join('?' for _ in batch)
        deleted += conn.execute(f'DELETE FROM {table} AS p WHERE {key} IN ({marks}) AND ' +
                                protection, batch).rowcount
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
    for version, byte_size in conn.execute(
            body_query, (SCAN_LIMIT,)):
        bodies.append(version)
        size += byte_size
        if size >= SCAN_BYTES:
            break
    for start in range(0, len(bodies), 256):
        batch = bodies[start:start + 256]
        marks = ','.join('?' for _ in batch)
        body_table, body_key = ('recommendation_policy_bodies', 'version') if modern else ('rec_policy_bases', 'base_version')
        conn.execute(f'DELETE FROM {body_table} WHERE {body_key} IN ({marks})', batch)
    return {'policies_deleted': deleted, 'bodies_deleted': len(bodies)}
