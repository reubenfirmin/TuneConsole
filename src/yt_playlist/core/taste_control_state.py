"""Retained taste controls and comparisons, separate from disposable preparation."""
import json
import uuid

from . import catalog, collection_recovery, genre_identity, taste_axes

CHECKPOINT = 'taste_settings_checkpoint_v1'
BASELINE = 'taste_comparison_baseline_v2'
KEYS = frozenset({CHECKPOINT,BASELINE})


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE name='taste_settings_checkpoints'").fetchone() is not None


@collection_recovery.pinned_read
def get(conn,key,default=None):
    table,kind = ('taste_settings_checkpoints','taste-checkpoint') if key==CHECKPOINT else ('taste_comparison_baselines','taste-comparison')
    row = conn.execute(f'SELECT * FROM {table} WHERE id=1').fetchone()
    if row is None:
        return default
    value = collection_recovery.read(row['recovery_filename'],row['recovery_digest'],row['recovery_bytes'],kind=kind,owner=1)
    if key==CHECKPOINT:
        weights = [{'axis':r['captured_axis'],'weight':r['weight'],'updated_at':r['updated_at']}
                   for r in conn.execute('SELECT * FROM taste_checkpoint_weights ORDER BY position')]
        return json.dumps({'saved_at':row['saved_at'],'params':value,'weights':weights})
    rows = list(conn.execute('SELECT e.*,COALESCE(c.identity_key,r.identity_key,e.unresolved_key) current_key '
        'FROM taste_comparison_entries e LEFT JOIN recordings r ON r.id=e.recording_id '
        'LEFT JOIN recording_redirects d ON d.retired_id=r.id LEFT JOIN recordings c ON c.id=d.recording_id ORDER BY position'))
    if len(value)!=len(rows):
        raise ValueError('Taste comparison traces do not match their ordered owner rows')
    return json.dumps([{'key':r['current_key'],'rank':r['rank'],'score':r['score'],'lane':r['lane'],
                       'lane_weight':r['lane_weight'],'trace':value[index]} for index,r in enumerate(rows)])


def put(conn,values,*,legacy=False):
    for key,raw in values.items():
        table,kind = ('taste_settings_checkpoints','taste-checkpoint') if key==CHECKPOINT else ('taste_comparison_baselines','taste-comparison')
        conn.execute(f'UPDATE {table} SET revision=revision WHERE 0')
        value = json.loads(raw) if raw else None
        if value is None:
            conn.execute(f'DELETE FROM {table}')
            continue
        if key==CHECKPOINT:
            if (not isinstance(value,dict) or set(value)!={'saved_at','params','weights'}
                    or not isinstance(value['params'],dict) or not isinstance(value['weights'],list)
                    or any(not isinstance(k,str) or not isinstance(v,str) for k,v in value['params'].items())):
                raise ValueError('Unsupported taste settings checkpoint')
            weights = []
            for position,weight in enumerate(value['weights']):
                if not isinstance(weight,dict) or set(weight)!={'axis','weight','updated_at'}:
                    raise ValueError('Unsupported checkpoint weight')
                axis = taste_axes.identity(conn,weight['axis'],create=True)
                weights.append((1,position,axis,weight['axis'],weight['weight'],weight['updated_at']))
            reference = collection_recovery.publish(conn,kind,1,value['params'])
            conn.execute('DELETE FROM taste_settings_checkpoints')
            conn.execute('INSERT INTO taste_settings_checkpoints VALUES (1,?,?,?,?,?)',
                         (uuid.uuid4().hex,value['saved_at'],*reference))
            conn.executemany('INSERT INTO taste_checkpoint_weights VALUES (?,?,?,?,?,?)',weights)
            continue
        if not isinstance(value,list):
            raise ValueError('Taste comparison baseline must be an ordered list')
        rows,traces = [],[]
        for position,item in enumerate(value):
            if not isinstance(item,dict) or set(item)!={'key','rank','score','trace','lane','lane_weight'}:
                raise ValueError('Unsupported taste comparison entry')
            if not isinstance(item['key'],str) or not item['key']:
                raise ValueError('Comparison entry requires its captured identity')
            key_now = catalog.resolve_key(conn,item['key'])
            recording = conn.execute('SELECT id FROM recordings WHERE identity_key=?',(key_now,)).fetchone() if key_now else None
            if recording is None and not legacy:
                raise ValueError('Comparison entry requires a known recording')
            rows.append((1,position,recording[0] if recording else None,None if recording else item['key'],
                         item['key'],item['rank'],item['score'],item['lane'],item['lane_weight']))
            traces.append(item['trace'])
        reference = collection_recovery.publish(conn,kind,1,traces)
        conn.execute('DELETE FROM taste_comparison_baselines')
        conn.execute('INSERT INTO taste_comparison_baselines VALUES (1,?,?,?,?)',(uuid.uuid4().hex,*reference))
        conn.executemany('INSERT INTO taste_comparison_entries VALUES (?,?,?,?,?,?,?,?,?)',rows)


def restored_weights(conn):
    """Restore captured identities; alias edits cannot reinterpret old checkpoints."""
    from .recommendation_settings import LANE_NAMES
    result,seen = [],set()
    for row in conn.execute('SELECT w.*,a.kind,a.genre_key FROM taste_checkpoint_weights w '
                            'JOIN taste_axes a ON a.id=w.axis_id ORDER BY position').fetchall():
        if row['captured_axis'].startswith('lane:') and row['captured_axis'][5:] not in LANE_NAMES:
            continue
        axis = row['axis_id']
        if row['kind']=='genre':
            key = genre_identity.current_key(conn,row['genre_key'])
            axis = taste_axes.identity(conn,'genre:'+key,create=True)
        if axis in seen:
            raise ValueError('Merged checkpoint axes require an explicit choice before restoring')
        seen.add(axis)
        result.append((axis,row['weight'],row['updated_at']))
    return result
