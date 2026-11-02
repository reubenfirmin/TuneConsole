"""Explicit resumable FAO preparation. No bulk conversion runs at startup."""
import argparse
from contextlib import closing
import importlib
import json
from pathlib import Path
import time

from .. import fao

BODY_OWNERS = ('policy_documents','evidence_ledger','metadata_delivery_files',
    'audio_annotation_files','album_description_files','sample_files','metadata_runtime_files',
    'group_documents','collection_recovery','recommendation_outputs','legacy_evidence',
    'metadata_destination_files','saved_preparation_files')
PREPARATIONS = ('profiles','embeddings','vibe_provenance','legacy_journals','policy_history')


def managed_roots(conn):
    """Finite domain inventory; formats and retention remain with their owners."""
    if not conn.execute('PRAGMA database_list').fetchone()[2]:
        raise ValueError('File conversion requires a file-backed database')
    from .. import file_owners, preparation_files
    roots = {name:module.directory(conn) for name,module in file_owners.owners().items()}
    for name in BODY_OWNERS:
        module = importlib.import_module('yt_playlist.core.'+name)
        roots[name] = module.directory(conn)
    for name,directory,_relations in preparation_files.domains():
        roots['preparation_'+name] = directory(conn)
    return roots


class Conversion:
    """Bound each tick by entries examined and admitted logical bytes.

    Compressed files are their own durable completion markers. Restart may
    rescan directory entries, but never repeats completed compression or holds
    a SQL/serving lock. The iterator owns and closes its directory handles.
    """
    def __init__(self, roots):
        self.roots = dict(roots)
        self.paths = self._paths()
        self.complete = False
        self.pending = None

    def _paths(self):
        for name,root in self.roots.items():
            for path in fao.walk_entries(root):
                yield name,path

    def step(self, *, entries=16, logical_bytes=32*1024*1024):
        if not 1<=entries<=128 or not 0<logical_bytes<=fao.MAX_BYTES:
            raise ValueError('Invalid conversion work budget')
        result = {'examined':0,'converted':0,'logical':0,'compressed':0,'allocated':0}
        for _ in range(entries):
            try:
                _domain,path = self.pending if self.pending is not None else next(self.paths)
                self.pending = None
            except StopIteration:
                self.complete = True
                break
            result['examined'] += 1
            if path is None:
                continue
            try:
                before = fao.stat(path)
                if before.compressed:
                    continue
                if before.logical_bytes>logical_bytes-result['logical']:
                    # Include this path in the next tick; a single oversized
                    # file requires an explicit larger per-file budget.
                    if before.logical_bytes>logical_bytes:
                        raise fao.FileBudgetExceeded('Managed file exceeds conversion tick budget: '+str(path))
                    self.pending = (_domain,path)
                    break
                if fao.convert(path,max_bytes=logical_bytes):
                    after = fao.stat(path)
                    result['converted'] += 1
                    for key,attribute in (('logical','logical_bytes'),('compressed','compressed_bytes'),('allocated','allocated_bytes')):
                        result[key] += getattr(after,attribute)
            except (FileNotFoundError,fao.FileChanged):
                continue  # Concurrent owner publication/expiry is retried later.
        return result

    def close(self):
        self.paths.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    parser.add_argument('--domain',choices=(*PREPARATIONS,'compression'),required=True)
    parser.add_argument('--batches',type=int,default=1)
    parser.add_argument('--logical-bytes',type=int,default=32*1024*1024,
                        help='Compression work budget per tick, at most 512 MiB')
    parser.add_argument('--assets',type=Path,action='append',default=[],
                        help='Additional managed asset tree, including packaged static/model resources')
    args = parser.parse_args()
    if args.batches<1:
        parser.error('--batches must be positive')
    if not 0<args.logical_bytes<=fao.MAX_BYTES:
        parser.error('--logical-bytes must be between 1 and '+str(fao.MAX_BYTES))
    from ..store import Store
    store = Store(args.database)
    try:
        store.init_schema()
        if args.domain=='compression':
            roots = managed_roots(store.conn)
            roots.update((str(path),path) for path in args.assets)
            with closing(Conversion(roots)) as work:
                for _ in range(args.batches):
                    print(json.dumps(work.step(logical_bytes=args.logical_bytes)),flush=True)
                    if work.complete:
                        break
        else:
            owner = importlib.import_module(__package__+'.'+args.domain)
            for _ in range(args.batches):
                kwargs = {'now':time.time()} if args.domain=='policy_history' else {}
                count = owner.prepare(store,**kwargs)
                print(json.dumps({'domain':args.domain,'converted':count}),flush=True)
                if not count:
                    break
    finally:
        store.conn.close()


if __name__=='__main__':
    main()
