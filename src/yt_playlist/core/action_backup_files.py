"""Exact playlist backup bytes retained by action history, including offline export."""
import hashlib
from pathlib import Path
from uuid import uuid4

from . import action_backup_inventory, collection_recovery, fao
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

    def retain(self, filename, *, expected):
        (encoded, digest, size), age = verified(filename, with_age=True)
        if (digest, size) != expected:
            raise ValueError('Action backup changed before publication')
        path = record_path(self.root, uuid4().hex)
        # The controller retains the domain root through the final SQL update.
        fao.write(path, fao.Content(encoded, metadata=age, max_bytes=MAX_BYTES))
        return str(path)


def inventory(conn):
    # Offline export can pin the installed database before its upgrade.
    if _predecessor(conn):
        return [dict(row) for row in conn.execute('SELECT * FROM action_backups ORDER BY action_id,position')]
    return [item for action, encoded in conn.execute("SELECT id,backup_inventory FROM actions "
                                                    "WHERE backup_inventory<>'[]' ORDER BY id")
            for item in action_backup_inventory.decode(action, encoded)]


def _predecessor(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='action_backups'").fetchone() is not None


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
        predecessor = _predecessor(conn)
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
                    target = record_path(collection_recovery.directory(conn), identity)
                    fao.write(target, fao.Content(raw, metadata=age, max_bytes=MAX_BYTES))
                    filename = str(target)
                else:
                    filename = _publish_encoded(collection_recovery.directory(conn),raw,row['digest'],age=age)
                if predecessor:
                    conn.execute('UPDATE action_backups SET retained_filename=? WHERE action_id=? AND position=?',
                                 (filename, row['action_id'], row['position']))
                else:
                    action_backup_inventory.relocate(conn, row['action_id'], row['position'], filename)
                conn.execute('UPDATE action_steps SET backup_filename=? WHERE action_id=? AND backup_status=\'verified\' '
                             'AND backup_digest=? AND backup_bytes=?',
                             (filename,row['action_id'],row['digest'],row['bytes']))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
