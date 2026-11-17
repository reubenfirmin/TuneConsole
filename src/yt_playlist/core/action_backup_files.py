"""Exact playlist backup bytes retained by action history, including offline export."""
import hashlib
from pathlib import Path

from . import collection_recovery, fao
from .action_backup_paths import record_path, record_id
from .evidence_ledger import canonical
from .policy_documents import _publish_encoded

FORMAT = 'action-backup-export-1'
MAX_BYTES = 64 * 1024 * 1024


def verified(filename,digest=None,size=None,*,with_age=False):
    with fao.read(filename) as stream:
        encoded = stream.read(MAX_BYTES+1)
        age = stream.info
    actual = hashlib.sha256(encoded).hexdigest()
    if not encoded or len(encoded)>MAX_BYTES or (digest is not None and actual!=digest) or (size is not None and len(encoded)!=size):
        raise ValueError('Action backup bytes do not match their committed reference')
    result = encoded,actual,len(encoded)
    return (result,age) if with_age else result


def retain(conn,filename):
    (encoded,digest,size),age = verified(filename,with_age=True)
    return _publish_encoded(collection_recovery.directory(conn),encoded,digest,age=age),digest,size


class ActionBackupFiles:
    def __init__(self, root):
        self.root = root

    def reference(self, filename, *, historical=False):
        if filename is None:
            return (None, None, None, 'none'), None
        if not isinstance(filename, str) or not filename:
            raise ValueError('Action backup requires a filename')
        try:
            encoded, digest, size = verified(filename)
        except FileNotFoundError:
            if historical:
                return (filename, None, None, 'missing_historical'), None
            raise ValueError('Action backup is missing; remote mutation was not admitted') from None
        import json
        value = json.loads(encoded)
        if (not isinstance(value, dict) or value.get('snapshot_version', 1) not in (1, 2)
                or not isinstance(value.get('tracks'), list) or not value.get('ytm_playlist_id')):
            raise ValueError('Unsupported playlist recovery backup')
        return (filename, digest, size, 'verified'), value

    def retain_reserved(self, identity, filename, *, expected):
        path = record_path(self.root, identity)
        (encoded, digest, size), age = verified(filename, with_age=True)
        if (digest, size) != expected:
            raise ValueError('Action backup changed after reservation')
        # The controller retains the domain root through the final SQL update.
        with fao.prepare(path, fao.Content(encoded, metadata=age, max_bytes=MAX_BYTES)) as pending:
            pending.publish()
        return str(path), digest, size


def inventory(conn):
    return [dict(row) for row in conn.execute('SELECT * FROM action_backups ORDER BY action_id,position')]


def export(conn,root):
    if not conn.in_transaction:
        raise ValueError('Action backup export requires a pinned database snapshot')
    root = Path(root)
    if root.resolve()==collection_recovery.directory(conn).resolve():
        raise ValueError('Action backup export requires a separate directory')
    records = []
    for row in inventory(conn):
        if row['availability']=='verified':
            (encoded,digest,_),age = verified(row['retained_filename'],row['digest'],row['bytes'],with_age=True)
            _publish_encoded(root,encoded,digest,age=age)
        records.append({key:value for key,value in row.items() if key!='retained_filename'})
    encoded = canonical({'format':FORMAT,'records':records})
    if len(encoded)>collection_recovery.MAX_BYTES:
        raise ValueError('Action backup manifest exceeds its byte budget')
    return _publish_encoded(root,encoded,hashlib.sha256(encoded).hexdigest())


def restore(conn,manifest):
    import json
    path = Path(manifest)
    encoded,digest,_ = verified(path)
    value = json.loads(encoded)
    if digest!=path.stem or canonical(value)!=encoded or set(value)!={'format','records'} or value['format']!=FORMAT:
        raise ValueError('Unsupported action backup manifest')
    if conn.in_transaction:
        raise ValueError('Action backup restore requires an idle connection')
    conn.execute('BEGIN IMMEDIATE')
    try:
        retained = inventory(conn)
        actual = [{key:item for key,item in row.items() if key!='retained_filename'} for row in retained]
        if value['records']!=actual:
            raise ValueError('Action backup manifest does not match retained owners')
        for row in retained:
            if row['availability']=='verified':
                source = path.parent.parent/row['digest'][:2]/(row['digest']+'.json')
                (raw,_,_),age = verified(source,row['digest'],row['bytes'],with_age=True)
                identity = record_id(row['retained_filename'])
                if identity is not None:
                    owner = conn.execute('SELECT action_id,position,content_digest,byte_size,file_status '
                        'FROM action_backup_writes WHERE id=?', (identity,)).fetchone()
                    if owner is None or tuple(owner) != (row['action_id'], row['position'], row['digest'], row['bytes'], 'ready'):
                        raise ValueError('Action backup has an inconsistent file identity')
                    target = record_path(collection_recovery.directory(conn), identity)
                    fao.write(target, fao.Content(raw, metadata=age, max_bytes=MAX_BYTES))
                    filename = str(target)
                else:
                    filename = _publish_encoded(collection_recovery.directory(conn),raw,row['digest'],age=age)
                conn.execute('UPDATE action_backups SET retained_filename=? WHERE action_id=? AND position=?',
                             (filename,row['action_id'],row['position']))
                conn.execute('UPDATE action_steps SET backup_filename=? WHERE action_id=? AND backup_status=\'verified\' '
                             'AND backup_digest=? AND backup_bytes=?',
                             (filename,row['action_id'],row['digest'],row['bytes']))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def relocate(conn,action_id,filename):
    row = conn.execute('SELECT retained_filename FROM action_backups WHERE action_id=? AND '
                       '(filename=? OR retained_filename=?)',(action_id,filename,filename)).fetchone()
    if row and row[0] is not None:
        return row[0]
    identity = record_id(filename)
    if identity is not None:
        row = conn.execute('SELECT b.retained_filename FROM action_backups b JOIN action_backup_writes f '
            'ON f.action_id=b.action_id AND f.position=b.position WHERE b.action_id=? AND f.id=? '
            "AND f.file_status='ready' AND b.availability='verified'", (action_id, identity)).fetchone()
        if row:
            return row[0]
    # Progress files can capture an older content-addressed retained location.
    # A cloned database has the same immutable digest under a different root.
    path = Path(filename)
    if path.suffix=='.json' and len(path.stem)==64 and all(char in '0123456789abcdef' for char in path.stem):
        row = conn.execute('SELECT retained_filename FROM action_backups WHERE action_id=? AND digest=? '
                           "AND availability='verified'",(action_id,path.stem)).fetchone()
        if row:
            return row[0]
    return filename
