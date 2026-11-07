"""Explicit experiment decisions retain immutable evidence under individual owners."""
import json

from . import collection_recovery

KEY = 'ppr_experiment_decisions'
KEYS = frozenset({KEY})


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='experiment_decisions'").fetchone() is not None


@collection_recovery.pinned_read
def records(conn):
    result = []
    for row in conn.execute('SELECT * FROM experiment_decisions ORDER BY id'):
        evidence = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
                                            kind='experiment-decision',owner=row['id'])
        result.append(restore(row, evidence))
    return result


def get(conn,key,default=None):
    values = records(conn)
    return json.dumps(values) if values else default


def put(conn,values,*,legacy=False):
    conn.execute('UPDATE experiment_decisions SET reason=reason WHERE 0')
    for raw in values.values():
        current = records(conn)
        rows = documents(conn)
        first_id = rows[-1]['id']+1 if rows else 1
        prepared = [(first_id+offset, item, collection_recovery.publish(conn, 'experiment-decision', first_id+offset, item['live_evidence']))
                    for offset, item in enumerate(prepare(raw, current, legacy=legacy))]
        register(conn, [row['id'] for row in rows], prepared)


def documents(conn):
    rows = [dict(row) for row in conn.execute('SELECT * FROM experiment_decisions ORDER BY id LIMIT 201')]
    if len(rows) > 200:
        raise ValueError('Experiment history exceeds its retained record limit')
    return rows


def restore(row, evidence):
    return {'decision':row['decision'],'cohort':row['cohort'],'reason':row['reason'],
            'at':row['created_at'],'powered':bool(row['powered']),'basis':row['basis'],'live_evidence':evidence}


def prepare(raw, current, *, legacy=False):
    value = json.loads(raw) if raw else []
    if not isinstance(value,list) or len(value)>200:
        raise ValueError('Experiment decisions require at most 200 ordered records')
    if current == value:
        return []
    if not legacy:
        overlap = min(len(current),max(0,len(value)-1))
        if len(value)!=min(200,len(current)+1) or (overlap and current[-overlap:]!=value[:overlap]):
            raise ValueError('Published experiment decisions cannot be rewritten')
        value = value[-1:]
    elif current:
        raise ValueError('Legacy experiment history already imported')
    for item in value:
        if (not isinstance(item,dict) or set(item)!={'decision','cohort','reason','at','powered','basis','live_evidence'}
                or type(item['powered']) is not bool or not isinstance(item['live_evidence'],dict)
                or set(item['live_evidence'])!={'ppr','cosine'}):
            raise ValueError('Unsupported experiment decision')
    return value


def register(conn, expected_ids, prepared):
    if [row[0] for row in conn.execute('SELECT id FROM experiment_decisions ORDER BY id LIMIT 201')] != expected_ids:
        raise ValueError('Experiment history changed before its decision was saved')
    conn.executemany('INSERT INTO experiment_decisions VALUES (?,?,?,?,?,?,?,?,?,?)',
        [(identity, item['decision'], item['cohort'], item['reason'], item['at'], int(item['powered']), item['basis'], *reference)
         for identity, item, reference in prepared])
    conn.execute('DELETE FROM experiment_decisions WHERE id NOT IN (SELECT id FROM experiment_decisions ORDER BY id DESC LIMIT 200)')
