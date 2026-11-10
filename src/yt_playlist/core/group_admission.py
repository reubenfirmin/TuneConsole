"""Persistence admission shared by group evidence publishers and collectors."""
from contextlib import contextmanager
import threading

from . import group_documents, policy_documents
from .activity import WorkBudgetExceeded, check_cancellable_work
from .db_writes import atomic_write
from .evidence_ledger import _mkdir


def source(store):
    return getattr(store, '_evidence_source', store)


def enabled(store):
    return getattr(store, '_compact_group_files', False)


@contextmanager
def admission(store, *, blocking=True):
    """Serialize this domain across processes without holding the serving lock."""
    store = source(store)
    if not enabled(store):
        yield
        return
    with store._lock:
        if not hasattr(store, '_group_root_admission'):
            store._group_root_admission = threading.RLock()
            store._group_root_depth = threading.local()
    if blocking:
        while not store._group_root_admission.acquire(timeout=.025):
            check_cancellable_work()
    elif not store._group_root_admission.acquire(blocking=False):
        raise WorkBudgetExceeded('Listening history preparation is using publication. Please retry.')
    try:
        if getattr(store._group_root_depth, 'value', 0):
            store._group_root_depth.value += 1
            try:
                yield
            finally:
                store._group_root_depth.value -= 1
            return
        root = group_documents.root_for_database(store._db_path, store._evidence_key)
        _mkdir(root)
        with (root / '.evidence.lock').open('a+b') as stream:
            unlock = policy_documents._try_file_lock(stream, exclusive=True)
            if unlock is None:
                raise WorkBudgetExceeded('Listening evidence publication is owned by another process')
            store._group_root_depth.value = 1
            try:
                yield
            finally:
                store._group_root_depth.value = 0
                unlock()
    finally:
        store._group_root_admission.release()


@contextmanager
def writer(store):
    store = source(store)
    with atomic_write(store):
        yield store.conn
