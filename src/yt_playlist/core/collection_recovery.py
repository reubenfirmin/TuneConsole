"""Immutable, versioned files owned by recoverable collection workflows."""
import hashlib
import json
from pathlib import Path
import tempfile
from functools import wraps

from .evidence_ledger import canonical
from .policy_documents import _publish_encoded
from . import policy_documents, fao
from .activity import check_cancellable_work
from .evidence_ledger import _sync_directory

FORMAT = 'collection-recovery-1'
KINDS = frozenset({'road-trip-draft','radio-session','library-sync','taste-settings',
                   'taste-checkpoint','taste-comparison','experiment-decision',
                   'spotify-import','playlist-recipe','playlist-canvas',
                   'action','action-draft','action-step','playlist-gc','spotify-transfer','tv-append',
                   'discovery-candidate'})
MAX_BYTES = 16 * 1024 * 1024
BACKUP_FORMAT = 'collection-recovery-backup-1'
OWNERS = {'road_trip_drafts':'road-trip-draft','radio_sessions':'radio-session',
          'sync_attempts':'library-sync','taste_model_state':'taste-settings',
          'taste_settings_checkpoints':'taste-checkpoint','taste_comparison_baselines':'taste-comparison',
          'experiment_decisions':'experiment-decision','spotify_import_jobs':'spotify-import',
          'rec_recipes':'playlist-recipe','cluster_canvas':'playlist-canvas',
          'actions':'action','action_drafts':'action-draft','action_steps':'action-step',
          'playlist_gc_attempts':'playlist-gc','spotify_transfer_receipts':'spotify-transfer',
          'tv_append_intents':'tv-append','discovered_artists':'discovery-candidate'}
OWNER_KEYS = {'discovered_artists':'artist_id'}


def root_for_database(database,memory_key):
    if database:
        path = Path(database).resolve()
        return path.with_name(path.name+'.collections')
    return Path(tempfile.gettempdir())/'tuneconsole-collections'/memory_key


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    return root_for_database(database,getattr(conn,'_policy_memory_key',str(id(conn))))


def pinned_read(method):
    @wraps(method)
    def wrapped(conn,*args,**kwargs):
        with policy_documents.retained_reader(directory(conn)):
            own_snapshot = not conn.in_transaction
            if own_snapshot:
                conn.execute('BEGIN')
            try:
                return method(conn,*args,**kwargs)
            finally:
                if own_snapshot:
                    conn.rollback()
    return wrapped


def publish(conn, kind, owner, value, *, age=None):
    if kind not in KINDS:
        raise ValueError('Unsupported collection recovery owner')
    encoded = canonical({'format':FORMAT,'kind':kind,'owner':str(owner),'state':value})
    if len(encoded)>MAX_BYTES:
        raise ValueError('Collection recovery exceeds its byte budget')
    digest = hashlib.sha256(encoded).hexdigest()
    filename = _publish_encoded(directory(conn),encoded,digest,age=age)
    return filename,digest,len(encoded)


def read(filename,digest,size,*,kind,owner,with_age=False):
    if kind not in KINDS or type(size) is not int or not 0<size<=MAX_BYTES:
        raise ValueError('Invalid collection recovery reference')
    path = Path(filename)
    with fao.open_reader(path) as stream:
        age = stream.info
        encoded = stream.read(size+1)
    if path.stem!=digest or len(encoded)!=size or hashlib.sha256(encoded).hexdigest()!=digest:
        raise ValueError('Collection recovery does not match its digest')
    document = json.loads(encoded)
    if (not isinstance(document,dict) or set(document)!={'format','kind','owner','state'}
            or document['format']!=FORMAT or document['kind']!=kind or document['owner']!=str(owner)
            or canonical(document)!=encoded):
        raise ValueError('Collection recovery belongs to another workflow or format')
    return (document['state'], age) if with_age else document['state']


def references(conn):
    for table,kind in OWNERS.items():
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        if 'recovery_filename' in columns:
            key = OWNER_KEYS.get(table,'id')
            for identity,filename,digest,size in conn.execute(
                    f'SELECT {key},recovery_filename,recovery_digest,recovery_bytes FROM {table} '
                    f'WHERE recovery_filename IS NOT NULL ORDER BY {key}'):
                yield table,identity,kind,filename,digest,size


def _manifest(root,value):
    encoded = canonical({'format':BACKUP_FORMAT,**value})
    if len(encoded)>MAX_BYTES:
        raise ValueError('Collection backup manifest exceeds its byte budget')
    return _publish_encoded(root,encoded,hashlib.sha256(encoded).hexdigest())


def _load_manifest(path):
    path = Path(path)
    with fao.open_reader(path) as stream:
        encoded = stream.read(MAX_BYTES+1)
    if len(encoded)>MAX_BYTES or hashlib.sha256(encoded).hexdigest()!=path.stem:
        raise ValueError('Collection backup manifest does not match its digest')
    document = json.loads(encoded)
    if not isinstance(document,dict) or document.get('format')!=BACKUP_FORMAT or canonical(document)!=encoded:
        raise ValueError('Unsupported collection backup')
    return document


def export(conn,root):
    if not conn.in_transaction:
        raise ValueError('Collection export requires a pinned database snapshot')
    root = Path(root)
    if root.resolve()==directory(conn).resolve():
        raise ValueError('Collection backup requires a separate directory')
    records,chunks = [],[]
    for table,identity,kind,filename,digest,size in references(conn):
        check_cancellable_work()
        value, age = read(filename,digest,size,kind=kind,owner=identity,with_age=True)
        encoded = canonical({'format':FORMAT,'kind':kind,'owner':str(identity),'state':value})
        copied = _publish_encoded(root,encoded,digest,age=age)
        records.append({'table':table,'id':identity,'kind':kind,'digest':digest,'bytes':size,
                        'relative_filename':str(Path(copied).relative_to(root))})
        if len(records)==512:
            chunks.append(Path(_manifest(root,{'type':'chunk','records':records})).stem)
            records = []
    if records:
        chunks.append(Path(_manifest(root,{'type':'chunk','records':records})).stem)
    return _manifest(root,{'type':'manifest','chunks':chunks})


def _digest(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def restore(conn,manifest):
    path = Path(manifest)
    root = path.parent.parent
    document = _load_manifest(path)
    if (set(document)!={'format','type','chunks'} or document['type']!='manifest'
            or not isinstance(document['chunks'],list) or any(not _digest(d) for d in document['chunks'])
            or len(set(document['chunks']))!=len(document['chunks'])):
        raise ValueError('Invalid collection backup manifest')
    records = {}
    for digest in document['chunks']:
        chunk = _load_manifest(root/digest[:2]/(digest+'.json'))
        if (set(chunk)!={'format','type','records'} or chunk['type']!='chunk'
                or not isinstance(chunk['records'],list) or not 1<=len(chunk['records'])<=512):
            raise ValueError('Invalid collection backup chunk')
        for record in chunk['records']:
            if (not isinstance(record,dict) or set(record)!={'table','id','kind','digest','bytes','relative_filename'}
                    or record['table'] not in OWNERS or record['kind']!=OWNERS[record['table']]
                    or not isinstance(record['id'],(str,int)) or isinstance(record['id'],bool)
                    or not _digest(record['digest']) or type(record['bytes']) is not int
                    or not 0<record['bytes']<=MAX_BYTES
                    or record['relative_filename']!=record['digest'][:2]+'/'+record['digest']+'.json'
                    or (record['table'],record['id']) in records):
                raise ValueError('Invalid collection backup owner')
            read(root/record['relative_filename'],record['digest'],record['bytes'],
                 kind=record['kind'],owner=record['id'])
            records[record['table'],record['id']] = record
    if conn.in_transaction:
        raise ValueError('Collection restore requires an idle connection')
    conn.execute('BEGIN IMMEDIATE')
    try:
        actual = {(table,identity):(kind,digest,size) for table,identity,kind,_,digest,size in references(conn)}
        expected = {key:(r['kind'],r['digest'],r['bytes']) for key,r in records.items()}
        if actual!=expected:
            raise ValueError('Collection backup does not match database owners')
        files = {}
        for key,record in records.items():
            value, age = read(root/record['relative_filename'],record['digest'],record['bytes'],
                             kind=record['kind'],owner=record['id'],with_age=True)
            files[key] = publish(conn,record['kind'],record['id'],value,age=age)[0]
        with policy_documents.coordinate_updates(conn, (table for table, _ in files)):
            for (table,identity),filename in files.items():
                key = OWNER_KEYS.get(table,'id')
                conn.execute(f'UPDATE {table} SET recovery_filename=? WHERE {key}=?',(filename,identity))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def collect_orphans(conn):
    if conn.in_transaction:
        raise ValueError('Collection cleanup requires an idle connection')
    root = directory(conn)
    if not root.exists():
        return 0
    conn.execute('BEGIN IMMEDIATE')
    removed = 0
    try:
        with (root/'.readers.lock').open('a+b') as stream:
            unlock = policy_documents._try_file_lock(stream,exclusive=True)
            if unlock is None:
                return 0
            try:
                owned = {row[3] for row in references(conn)}
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='action_backups'").fetchone():
                    owned.update(row[0] for row in conn.execute('SELECT retained_filename FROM action_backups WHERE retained_filename IS NOT NULL'))
                for path in root.glob('*/*.json'):
                    check_cancellable_work()
                    if str(path) not in owned:
                        removed += fao.delete(path, missing_ok=True)
            finally:
                unlock()
        return removed
    finally:
        conn.rollback()


def maintain(store):
    """Daily local cleanup; paused startup and historical schemas retain files."""
    with store._lock:
        if store.activity_paused or not any(row[1]=='recovery_filename'
                for row in store.conn.execute('PRAGMA table_info(road_trip_drafts)')):
            return 0
        return collect_orphans(store.conn)
