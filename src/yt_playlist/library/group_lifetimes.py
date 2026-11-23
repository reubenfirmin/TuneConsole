"""Typed group and recommendation references at listening evidence boundaries.

File evidence publication and its root retirement share one domain lock. SQL
roots commit before a file is acknowledged; an interrupted write can leave a
conservative root, never a recorded offer whose group history was collected.
"""
import threading
import time

from yt_playlist.core import group_evidence
from yt_playlist.core.prepared_references import FIELDS, PREPARED, context
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.group_admission import source, admission, writer
from yt_playlist.rec import recommendation_roots
from yt_playlist.controllers.pending_roots import PendingRoots
from yt_playlist.core import group_documents
from yt_playlist.core.pending_lifetimes import PendingLifetimes


def pending_roots(store):
    return PendingRoots(PendingLifetimes(group_documents.root_for_database(store._db_path, store._evidence_key) / '.pending'),
        admission=lambda **kw: admission(store, **kw), writer=lambda: writer(store),
        retain=lambda identity, captured, at: retain(store, 'pending', identity, captured, at),
        release=lambda identity: release(store, 'pending', identity),
        before_close=getattr(store.conn, '_before_close_callbacks', None))

def retain(store, kind, identity, captured, at, *, strict=None, ledger=None):
    if kind in {'offer', 'response', 'checkpoint'}:
        # Listening evidence owns displayed descriptors or its own frozen Album
        # and Radio inputs. It cannot extend the life of musical group history.
        return False
    value = context(store, captured, ledger=ledger)
    generation, versions = group_evidence.references(value)
    recommendations = recommendation_roots.references(value)
    if generation is None and not versions and not recommendations:
        return False
    return source(store).group_evidence.retain(kind, identity, value, at,
        strict=generation is not None if strict is None else strict, recommendations=recommendations)


def release(store, kind, identity):
    source(store).group_evidence.release(kind, identity)


def pending(store, captured, at):
    """A unique root belongs to one admitted capture, including cancelled jobs."""
    if isinstance(captured, dict) and not captured:
        return None
    sql_only = set(captured) == {'recommendation_generations'}
    return _pending(store, lambda: captured, at, sql_only=sql_only)[1]


def pending_head(store, surfaces, at):
    """Select and retain one current publication in the same writer transaction.

    Reserve the OS lifetime before taking the serving lock. A head switch or
    collector cannot create a gap between observing the publication and owning
    the reader reference. The returned close callback is idempotent.
    """
    if (not isinstance(surfaces, (list, tuple)) or not 1 <= len(surfaces) <= 128
            or any(not isinstance(surface, str) or not surface or len(surface.encode()) > 1024 for surface in surfaces)):
        raise ValueError('Invalid recommendation reader surfaces')
    if getattr(store, '_read_only', False):
        # The private reader already pins SQLite and the output-file lifetime.
        # Its head can precede the writer's current head; selecting through the
        # writer here would introduce a generation absent from this snapshot.
        for surface in surfaces:
            row = store.recommendations.head(surface)
            if row is not None:
                return row['id'], None
        return None, None
    store = source(store)
    def resolve():
        for surface in surfaces:
            row = store.recommendations.head(surface)
            if row is not None:
                return {'recommendation_generations': [row['id']]}
        return {}

    # A fallback reader has no publication to retain. In particular it must
    # remain usable while maintenance pauses SQL writes. This is only an
    # absence check: a real head is selected again inside _pending's writer
    # transaction, preserving its atomic capture against replacement/cleanup.
    with store._lock:
        if not resolve():
            return None, None
    # This resolver and its root reference SQL publications only. Their atomic
    # writer already serializes with SQL retirement; no evidence file is read.
    # Do not wait for an unrelated historical evidence inventory to finish.
    captured, close = _pending(store, resolve, at, sql_only=True)
    generations = captured.get('recommendation_generations', [])
    return generations[0] if generations else None, close


def _pending(store, resolve, at, *, sql_only=False):
    store = source(store)
    return pending_roots(store).capture(resolve, at, sql_only=sql_only)


def reap_pending(store):
    """Retire a bounded batch only when the capture's OS lifetime has ended."""
    store = source(store)
    from yt_playlist.repos.group_publication import BATCH_ROWS
    with admission(store):
        after = getattr(store, '_group_pending_after', '')
        identities = store.group_evidence.identities('pending', after, BATCH_ROWS)
        pending_roots(store).reap(identities)
        store._group_pending_after = identities[-1] if len(identities) == BATCH_ROWS else ''


def retired(store, offers, checkpoints):
    """The ledger calls this only after its protected records were removed."""
    from yt_playlist.repos.group_publication import BATCH_ROWS
    for kind, identities in (('offer', offers), ('checkpoint', checkpoints)):
        for offset in range(0, len(identities), BATCH_ROWS):
            source(store).group_evidence.retire(kind, identities[offset:offset + BATCH_ROWS])
    source(store).group_evidence.collect_mix_dictionary()


def maintain_retention(store):
    """Give each retirement domain its own bounded, resumable turn.

    This work does not pause activity recording or hold recovery intent open.
    Each collector has its own budget. Retirement and completed unlinks survive
    restarts.
    """
    store = source(store)
    result = dict(pending=False, recommendation_rows=0, generations=0, pools=0,
                  output_files=0, output_bytes=0, checked_files=0, group_steps=0)
    deadline = 0

    def exhausted():
        # Yield only after the owner has committed/advanced its prefix. The
        # elapsed-time boundary cannot interrupt SQL and roll that prefix back.
        if time.monotonic() >= deadline:
            result['pending'] = True
            return True
        return False

    def sql_turn(callback):
        try:
            # Each prefix commits durably and releases the serving lock. Copy
            # committed WAL pages once per bounded turn instead of per prefix.
            while progress := callback(checkpoint=False):
                result['recommendation_rows'] += progress.rows
                result['generations'] += progress.generations
                result['pools'] += progress.pools
                if exhausted():
                    return
        finally:
            store.recommendations.checkpoint_retirement()

    def group_cleanup():
        store.group_evidence.collect_unpublished_mixes()
        reap_pending(store)
        # Admission/reaping must not consume the whole turn before a build
        # prefix advances. Finish at least one prefix even on a slow disk.
        while store.taste_groups.cleanup_builds():
            result['group_steps'] += 1
            if exhausted():
                return

    def group_history():
        while store.taste_groups.retire_history():
            result['group_steps'] += 1
            result['pending'] = True
            if exhausted():
                return

    def files(*, groups=False):
        attribute = '_group_output_collector' if groups else '_recommendation_output_collector'
        collector = getattr(store, attribute, None)
        if collector is None:
            collector = store.group_evidence.output_collector(groups=groups)
            setattr(store, attribute, collector)
        while True:
            progress = store.group_evidence.collect_output_files(collector)
            prefix = 'group_' if groups else ''
            for name, value in (('output_files', progress.files), ('output_bytes', progress.byte_size),
                                ('checked_files', progress.checked)):
                result[prefix + name] = result.get(prefix + name, 0) + value
            if progress.blocked:
                result['pending'] = True
                return
            if not progress.pending:
                delattr(store, attribute)
                return
            if exhausted():
                return

    turns = [(lambda: sql_turn(store.recommendations.cleanup_generations), .1),
             (lambda: sql_turn(store.recommendations.cleanup_pools), .1), (files, .25),
             (group_cleanup, .25), (group_history, .25), (lambda: files(groups=True), .1)]
    with store._lock:
        if not hasattr(store, '_retention_lock'):
            store._retention_lock = threading.Lock()
    if not store._retention_lock.acquire(blocking=False):
        result['pending'] = True
        return result
    try:
        for callback, seconds in turns:
            deadline = time.monotonic() + seconds
            try:
                callback()
            except WorkBudgetExceeded:
                result['pending'] = True
    finally:
        store._retention_lock.release()
    store._retention_progress = result
    return result


def close_retention(store):
    """Release any directory iterator after the worker has stopped."""
    for attribute in ('_recommendation_output_collector', '_group_output_collector'):
        collector = getattr(store, attribute, None)
        if collector is not None:
            collector.close()
            delattr(store, attribute)
