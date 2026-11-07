"""Bounded idle collection for established file owners, never live-body expiry."""
from yt_playlist.core import file_collection
from .. import (fao, policy_documents, metadata_delivery_files, audio_annotation_files,
               album_description_files, sample_files, legacy_evidence,
               metadata_runtime_files, playback_inbox)
from ..db_writes import reserved_write


def _owners(*relations):
    def required(conn, path):
        return any(conn.execute(f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1',
                                (str(path),)).fetchone() is not None
                   for table,column in relations
                   if conn.execute('SELECT 1 FROM sqlite_schema WHERE name=?',(table,)).fetchone())
    return required


def contracts(conn):
    yield policy_documents.directory(conn),_owners(*[(table,'configuration_filename') for table in policy_documents.OWNERS])
    yield metadata_delivery_files.directory(conn),metadata_delivery_files.owns_file
    yield audio_annotation_files.directory(conn,getattr(conn,'_policy_memory_key',str(id(conn)))),_owners(
        ('audio_annotations','model_output_filename'))
    yield album_description_files.directory(conn),_owners(*[(table,fields[1]) for table,fields in album_description_files.OWNERS.items()])
    yield sample_files.directory(conn,getattr(conn,'_policy_memory_key',str(id(conn)))),_owners(('sample_attributes','filename'))
    yield legacy_evidence.directory(conn),_owners(*[(table,'filename') for table in legacy_evidence.DOMAINS.values()])
    # Named files without committed owners receive the ordinary 30-day grace.
    # Active publications, old snapshots and current SQL heads remain fenced.
    yield metadata_runtime_files.directory(conn),metadata_runtime_files.owns_file
    yield playback_inbox.directory(conn),playback_inbox.owns_file


class Orphans:
    def __init__(self,conn):
        self.contracts = list(contracts(conn))
        self.position = 0
        self.paths = None

    def step(self,store,now,limit=16):
        root,required = self.contracts[self.position]
        if self.paths is None:
            self.paths = file_collection.walk_entries(root)
        removed = 0
        for _ in range(limit):
            try:
                path = next(self.paths)
            except StopIteration:
                self.paths.close()
                self.paths = None
                self.position = (self.position+1)%len(self.contracts)
                break
            if path is None:
                continue
            try:
                info = fao.read(path).info
                if not (info.ctime_ns / 1e9 <= now - 30*86400):
                    continue
                with file_collection.collector(root) as admitted:
                    if not admitted:
                        continue
                    with reserved_write(store):
                        # Existing snapshots and new publication are fenced.
                        if not required(store.conn,path):
                            removed += fao.delete(path,expected=info.identity,missing_ok=True)
            except (FileNotFoundError,fao.FileChanged):
                continue
        return removed

    def close(self):
        if self.paths is not None:
            self.paths.close()
            self.paths = None


class Pending:
    """Crash-left unpublished bytes, after seven days; active stages hold leases."""
    def __init__(self,roots):
        self.paths = self._paths(roots)
        self.complete = False

    def _paths(self,roots):
        for root in set(roots):
            yield from file_collection.walk_entries(root,pending=True)

    def step(self,now,limit=16):
        removed = 0
        for _ in range(limit):
            try:
                path = next(self.paths)
            except StopIteration:
                self.complete = True
                break
            if path is None or not path.name.startswith('.fao-pending-'):
                continue
            try:
                info = path.stat()
                if info.st_ctime <= now-7*86400:
                    removed += fao.delete(path, missing_ok=True)
            except FileNotFoundError:
                pass
        return removed

    def close(self):
        self.paths.close()


def maintain(store,now):
    work = getattr(store,'_managed_file_orphans',None)
    if work is None:
        with store._lock:
            work = store._managed_file_orphans = Orphans(store.conn)
    removed = work.step(store,now)
    if store._db_path is not None:
        pending = getattr(store,'_managed_file_pending',None)
        if pending is None:
            from .file_bodies import managed_roots
            with store._lock:
                pending = store._managed_file_pending = Pending(managed_roots(store.conn).values())
        removed += pending.step(now)
        if pending.complete:
            pending.close()
            del store._managed_file_pending
    return removed


def close(store):
    for name in ('_managed_file_orphans','_managed_file_pending'):
        work = getattr(store,name,None)
        if work is not None:
            work.close()
            delattr(store,name)
