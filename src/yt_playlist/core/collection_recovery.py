"""Collection recovery compatibility API and command composition.

SQL references live in repos.collection_recovery, backup interpretation in
collection_backup, file operations in collection_backup_files, and export/
restore sequencing in controllers.collection_recovery.
"""
import hashlib
from pathlib import Path
import tempfile
from functools import wraps

from .policy_documents import _publish_encoded
from . import policy_documents
from .collection_files import FORMAT, KINDS, MAX_BYTES, RecoveryFiles, encode, read, record_id
from .collection_owners import OWNERS, OWNER_KEYS
from .collection_backup import FORMAT as BACKUP_FORMAT
from yt_playlist.repos.collection_recovery import references


def root_for_database(database, memory_key):
    if database:
        path = Path(database).resolve()
        return path.with_name(path.name + '.collections')
    return Path(tempfile.gettempdir()) / 'tuneconsole-collections' / memory_key


def directory(conn):
    database = conn.execute('PRAGMA database_list').fetchone()[2]
    return root_for_database(database, getattr(conn, '_policy_memory_key', str(id(conn))))


def pinned_read(method):
    """Protect referenced files for a read without opening a SQL transaction."""
    @wraps(method)
    def wrapped(conn, *args, **kwargs):
        with policy_documents.retained_reader(directory(conn)):
            return method(conn, *args, **kwargs)
    return wrapped


def publish(conn, kind, owner, value, *, age=None):
    return publish_at(directory(conn), kind, owner, value, age=age)


def publish_at(root, kind, owner, value, *, age=None):
    """Installed preparation/recovery format; runtime writers use reserved IDs."""
    encoded = encode(kind, owner, value)
    digest = hashlib.sha256(encoded).hexdigest()
    filename = _publish_encoded(root, encoded, digest, age=age)
    return filename, digest, len(encoded)


def _command(conn, root):
    from yt_playlist.controllers.collection_recovery import CollectionRecovery
    from yt_playlist.repos.collection_recovery import CollectionRecovery as Repository
    from .collection_backup_files import BackupFiles
    return CollectionRecovery(Repository(conn), BackupFiles(directory(conn), root))


def export(conn, root):
    return _command(conn, root).export()


def restore(conn, manifest):
    return _command(conn, Path(manifest).parent.parent).restore(manifest)
