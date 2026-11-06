"""Bounded, restartable traversal of flat domain-owned file shards."""
from collections import OrderedDict
from contextlib import contextmanager
import json
import os
from pathlib import Path
from threading import RLock

from . import fao
from .evidence_ledger import canonical

_CURSORS = OrderedDict()
_LOCK = RLock()


def walk_entries(root, *, pending=False, directories=False, max_depth=None):
    """Stream a tree; skipped entries also consume the caller's work budget."""
    root = Path(root)
    if not root.is_dir():
        return
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.is_symlink():
                yield None
            elif entry.name.startswith('.'):
                yield Path(entry.path) if pending and entry.name.startswith('.fao-pending-') and entry.is_file() else None
            elif entry.is_dir(follow_symlinks=False):
                yield Path(entry.path) if directories else None
                if max_depth is None or max_depth > 0:
                    yield from walk_entries(entry.path, pending=pending, directories=directories,
                                            max_depth=None if max_depth is None else max_depth-1)
            elif entry.is_file(follow_symlinks=False):
                yield Path(entry.path)
            else:
                yield None


@contextmanager
def collector(root):
    """A collector cannot invalidate a file referenced by an older SQL view."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.readers.lock').open('a+b') as stream:
        unlock = fao._try_file_lock(stream, exclusive=True)
        try:
            yield unlock is not None
        finally:
            if unlock is not None:
                unlock()


class Cursor:
    def __init__(self, root, name, valid_shard, prefixes):
        self.root, self.valid_shard, self.prefixes = root, valid_shard, prefixes
        self.checkpoint = root / name
        saved = json.loads(fao.read(self.checkpoint).bytes) if self.checkpoint.exists() else {}
        self.shard, self.after = saved.get('shard'), saved.get('after_shard', '')
        self.prefix = saved.get('prefix', 0)
        if type(self.prefix) is not int or not 0 <= self.prefix < len(prefixes):
            raise ValueError('Invalid file collection domain cursor')
        if (self.shard is not None and not valid_shard(self.shard)
                or self.after != '' and not valid_shard(self.after)):
            raise ValueError('Invalid file collection shard cursor')
        self.paths = self.folders = None
        self.first = self.following = None
        self.finished = False

    def next(self):
        """Charge files, skipped entries, shard discovery and transitions alike."""
        if self.shard is not None:
            if self.paths is None:
                self.paths = walk_entries(self.root / self.prefixes[self.prefix] / self.shard, max_depth=0)
            try:
                return next(self.paths)
            except StopIteration:
                self.paths.close()
                self.paths = None
                self.after, self.shard = self.shard, None
                return None
        if self.folders is None:
            self.folders = walk_entries(self.root / self.prefixes[self.prefix], directories=True, max_depth=0)
        try:
            path = next(self.folders)
        except StopIteration:
            self.folders.close()
            self.folders = None
            if self.following is None and len(self.prefixes) > 1:
                self.prefix = (self.prefix + 1) % len(self.prefixes)
                self.after = ''
            else:
                # A caller draining a private, stable directory can stop at
                # this boundary. Runtime collectors continue around the ring.
                self.finished = self.following is None
                self.shard = self.following or self.first
            self.first = self.following = None
            return None
        if path is not None and self.valid_shard(path.name) and path.is_dir():
            name = path.name
            if self.first is None or name < self.first:
                self.first = name
            if name > self.after and (self.following is None or name < self.following):
                self.following = name
        return None

    def save(self):
        saved = {'shard': self.shard, 'after_shard': self.after}
        if len(self.prefixes) > 1:
            saved['prefix'] = self.prefix
        fao.write(self.checkpoint, canonical(saved))

    def close(self):
        for stream in (self.paths, self.folders):
            if stream is not None:
                stream.close()
        self.paths = self.folders = None


@contextmanager
def collection(conn, root, *, name, valid_shard, prefixes=('',)):
    # Keep at most two live directory streams per owner and eight owners in all.
    # Restart repeats the current shard; completed shard progress survives it.
    with _LOCK:
        prefixes = tuple(prefixes)
        key = conn, root, name, prefixes
        cursor = _CURSORS.pop(key, None)
        if cursor is None:
            cursor = Cursor(root, name, valid_shard, prefixes)
            if len(_CURSORS) == 8:
                _CURSORS.popitem(last=False)[1].close()
            if hasattr(conn, '_close_callbacks'):
                def release():
                    with _LOCK:
                        if _CURSORS.get(key) is cursor:
                            del _CURSORS[key]
                        cursor.close()
                conn._close_callbacks.append(release)
        _CURSORS[key] = cursor
        yield cursor
