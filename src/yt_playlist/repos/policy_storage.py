"""SQLite storage for shared policies; callers own transactions and serving locks."""
import json
from itertools import chain

from yt_playlist.core.policy_config import BASE_FIELD
from yt_playlist.core.db_space import require_database_space

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
    """Conservatively keep policy IDs anywhere in persisted JSON, including sessions."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('pool_policy', 'model_version') and isinstance(child, (str, int)):
                yield str(child)
            yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)
    elif isinstance(value, str) and len(value) == 16:
        yield value


def protected_references(conn):
    from yt_playlist.repos.proposal_cache import logical_rows

    protected = set()
    values = (row['payload'] for row in logical_rows(conn))
    for raw in chain(values, (row[0] for row in conn.execute('SELECT value FROM settings'))):
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            value = raw
        protected.update(references(value))
    return protected


def retention_candidates(conn, now, *, limit=SCAN_LIMIT, cursor_key=CURSOR):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (cursor_key,)).fetchone()
    try:
        cursor = json.loads(row[0]) if row else None
        if not (isinstance(cursor, list) and len(cursor) == 2
                and isinstance(cursor[0], (float, int)) and isinstance(cursor[1], str)):
            cursor = None
    except (ValueError, TypeError):
        cursor = None
    after = ' AND (created_at,model_version)>(?,?)' if cursor else ''
    rows, size = [], 0
    for created_at, version, byte_size in conn.execute(
            'SELECT created_at,model_version,length(CAST(configuration AS BLOB)) '
            'FROM rec_serving_policies WHERE created_at<?' + after +
            ' ORDER BY created_at,model_version LIMIT ?', (now - GRACE_SECONDS, *(cursor or ()), limit)):
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
    deleted = 0
    for start in range(0, len(versions), 256):
        batch = versions[start:start + 256]
        marks = ','.join('?' for _ in batch)
        deleted += conn.execute(f'DELETE FROM rec_serving_policies AS p WHERE model_version IN ({marks}) AND ' +
                                UNSERVED, batch).rowcount
    if cursor:
        conn.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (cursor_key, json.dumps(cursor)))
    else:
        conn.execute('DELETE FROM settings WHERE key=?', (cursor_key,))
    bodies, size = [], 0
    for version, byte_size in conn.execute(
            'SELECT b.base_version,b.byte_size FROM rec_policy_bases b WHERE b.served=0 AND NOT EXISTS ('
            'SELECT 1 FROM rec_policy_variants v WHERE v.base_version=b.base_version) LIMIT ?', (SCAN_LIMIT,)):
        bodies.append(version)
        size += byte_size
        if size >= SCAN_BYTES:
            break
    for start in range(0, len(bodies), 256):
        batch = bodies[start:start + 256]
        marks = ','.join('?' for _ in batch)
        conn.execute(f'DELETE FROM rec_policy_bases WHERE base_version IN ({marks})', batch)
    return {'policies_deleted': deleted, 'bodies_deleted': len(bodies)}
