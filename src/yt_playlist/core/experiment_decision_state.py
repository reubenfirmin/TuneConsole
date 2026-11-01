"""Explicit experiment decisions retain immutable evidence under individual owners."""
import json

from . import collection_recovery

KEYS = frozenset({'ppr_experiment_decisions'})


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='experiment_decisions'").fetchone() is not None


@collection_recovery.pinned_read
def records(conn):
    result = []
    for row in conn.execute('SELECT * FROM experiment_decisions ORDER BY id'):
        evidence = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],
                                            kind='experiment-decision',owner=row['id'])
        result.append({'decision':row['decision'],'cohort':row['cohort'],'reason':row['reason'],
            'at':row['created_at'],'powered':bool(row['powered']),'basis':row['basis'],'live_evidence':evidence})
    return result


def get(conn,key,default=None):
    values = records(conn)
    return json.dumps(values) if values else default


def put(conn,values,*,legacy=False):
    conn.execute('UPDATE experiment_decisions SET reason=reason WHERE 0')
    for raw in values.values():
        value = json.loads(raw) if raw else []
        if not isinstance(value,list) or len(value)>200:
            raise ValueError('Experiment decisions require at most 200 ordered records')
        current = records(conn)
        if current==value:
            continue
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
            identity = conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM experiment_decisions').fetchone()[0]
            reference = collection_recovery.publish(conn,'experiment-decision',identity,item['live_evidence'])
            conn.execute('INSERT INTO experiment_decisions VALUES (?,?,?,?,?,?,?,?,?,?)',
                (identity,item['decision'],item['cohort'],item['reason'],item['at'],int(item['powered']),item['basis'],*reference))
        conn.execute('DELETE FROM experiment_decisions WHERE id NOT IN (SELECT id FROM experiment_decisions ORDER BY id DESC LIMIT 200)')
