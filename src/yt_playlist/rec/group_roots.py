"""Typed group and recommendation references at listening evidence boundaries.

File evidence publication and its root retirement share one domain lock. SQL
roots commit before a file is acknowledged; an interrupted write can leave a
conservative root, never a recorded offer whose group history was collected.
"""
from contextlib import contextmanager
import threading
import uuid
import sqlite3

from yt_playlist.core import group_documents, group_evidence, policy_documents
from yt_playlist.core.prepared_references import FIELDS, PREPARED, context, replace_prepared
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_cancellable_work
from yt_playlist.core.evidence_ledger import _mkdir
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.rec import recommendation_roots

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


def retain(store, kind, identity, captured, at, *, strict=None, ledger=None):
    if kind in {'offer', 'response', 'checkpoint'}:
        # Listening evidence owns displayed descriptors or its own frozen Album
        # and Radio inputs. It cannot extend the life of musical group history.
        return False
    if not enabled(store):
        return False
    value = context(store, captured, ledger=ledger)
    generation, versions = group_evidence.references(value)
    recommendations = recommendation_roots.references(value) if recommendation_roots.enabled(store) else []
    if generation is None and not versions and not recommendations:
        return False
    with writer(store) as conn:
        changed = group_evidence.retain(conn, kind, identity, value, at,
                                        strict=generation is not None if strict is None else strict)
        if recommendations:
            recommendation_roots.retain(conn, kind, identity, recommendations)
        return changed


def release(store, kind, identity):
    if enabled(store):
        with writer(store) as conn:
            group_evidence.release(conn, kind, identity)
            if recommendation_roots.enabled(store):
                from yt_playlist.repos import recommendation_publication
                recommendation_publication.release(conn, kind, identity)


def pending(store, captured, at):
    """A unique root belongs to one admitted capture, including cancelled jobs."""
    return _pending(store, lambda: captured, at)[1]


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
    if not recommendation_roots.enabled(store):
        return None, None

    def resolve():
        for surface in surfaces:
            row = store.conn.execute('SELECT generation_id FROM recommendation_heads WHERE surface=?',
                                      (surface,)).fetchone()
            if row is not None:
                return {'recommendation_generations': [row[0]]}
        return {}

    # A fallback reader has no publication to retain. In particular it must
    # remain usable while maintenance pauses SQL writes. This is only an
    # absence check: a real head is selected again inside _pending's writer
    # transaction, preserving its atomic capture against replacement/cleanup.
    with store._lock:
        if not resolve():
            return None, None
    captured, close = _pending(store, resolve, at)
    generations = captured.get('recommendation_generations', [])
    return generations[0] if generations else None, close


def _pending(store, resolve, at):
    if not enabled(store):
        return {}, None
    store = source(store)
    identity = uuid.uuid4().hex
    directory = group_documents.root_for_database(store._db_path, store._evidence_key) / '.pending'
    _mkdir(directory)
    path = directory / (identity + '.lock')
    stream = path.open('x+b')
    unlock = policy_documents._try_file_lock(stream, exclusive=True)
    if unlock is None:
        stream.close()
        raise WorkBudgetExceeded('Musical group capture lifetime could not be reserved')
    try:
        with admission(store, blocking=False):
            with writer(store):
                captured = resolve()
                retained = retain(store, 'pending', identity, captured, at)
    except BaseException:
        unlock()
        stream.close()
        path.unlink(missing_ok=True)
        raise
    if not retained:
        unlock()
        stream.close()
        path.unlink(missing_ok=True)
        return captured, None

    lifetime, closed = threading.Lock(), False

    def close():
        nonlocal closed
        # Commit retirement before releasing the OS lifetime fence. A crash
        # releases the file lock; maintenance can then prove this capture died.
        with lifetime:
            if closed:
                return
            retired = False
            try:
                with admission(store, blocking=False):
                    release(store, 'pending', identity)
                    retired = True
            except (WorkBudgetExceeded, WorkClosing):
                # The unlocked lifetime file lets the bounded reaper retire
                # this conservative SQL root after publication becomes free or
                # another owner resumes maintenance after shutdown.
                pass
            except sqlite3.OperationalError as error:
                if getattr(error, 'sqlite_errorcode', 0) & 255 not in (
                        sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
            finally:
                unlock()
                stream.close()
                closed = True
                if callbacks is not None:
                    callbacks.remove(close)
            if retired:
                path.unlink(missing_ok=True)
    callbacks = getattr(store.conn, '_before_close_callbacks', None)
    if callbacks is not None:
        callbacks.append(close)
    return captured, close


def reap_pending(store):
    """Retire a bounded batch only when the capture's OS lifetime has ended."""
    store = source(store)
    from yt_playlist.repos.group_publication import BATCH_ROWS
    with admission(store):
        after = getattr(store, '_group_pending_after', '')
        with store._lock:
            identities = [row[0] for row in store.conn.execute(
                "SELECT external_key FROM group_evidence_owners WHERE kind='pending' AND external_key>? "
                'ORDER BY external_key LIMIT ?', (after, BATCH_ROWS))]
        directory = group_documents.root_for_database(store._db_path, store._evidence_key) / '.pending'
        for identity in identities:
            if len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
                continue
            path = directory / (identity + '.lock')
            try:
                stream = path.open('r+b')
            except FileNotFoundError:
                # An unrecognized historical owner lacks a lifetime proof.
                continue
            with stream:
                unlock = policy_documents._try_file_lock(stream, exclusive=True)
                if unlock is None:
                    continue
                try:
                    release(store, 'pending', identity)
                finally:
                    unlock()
            path.unlink(missing_ok=True)
        store._group_pending_after = identities[-1] if len(identities) == BATCH_ROWS else ''


def retired(store, offers, checkpoints):
    """The ledger calls this only after its protected records were removed."""
    if not enabled(store):
        return
    from yt_playlist.repos.group_publication import BATCH_ROWS
    from yt_playlist.core import mix_choices
    for kind, identities in (('offer', offers), ('checkpoint', checkpoints)):
        for offset in range(0, len(identities), BATCH_ROWS):
            with writer(store) as conn:
                if kind == 'offer':
                    mix_choices.retire(conn, identities[offset:offset + BATCH_ROWS])
                for identity in identities[offset:offset + BATCH_ROWS]:
                    group_evidence.release(conn, kind, identity)
                    if recommendation_roots.enabled(store):
                        from yt_playlist.repos import recommendation_publication
                        recommendation_publication.release(conn, kind, identity)
    with writer(store) as conn:
        mix_choices.collect_dictionary(conn)


def maintain_retention(store):
    """Give each retirement domain its own bounded, resumable turn.

    This work does not pause activity recording or hold recovery intent open.
    A busy group traversal cannot consume the recommendation/file collectors'
    budgets. SQL retirement fences and completed unlinks survive restarts.
    """
    store = source(store)
    result = dict(pending=False, recommendation_rows=0, generations=0, pools=0,
                  output_files=0, output_bytes=0, checked_files=0, group_steps=0)
    if not enabled(store):
        return result
    from yt_playlist.core.activity import work_budget
    from yt_playlist.core.db_checkpoint import checkpoint_wal
    from yt_playlist.core import group_documents, recommendation_outputs
    from yt_playlist.repos import compact_group_publication, group_retention, recommendation_publication

    def sql_turn(callback):
        try:
            # Each prefix commits durably and releases the serving lock. Copy
            # committed WAL pages once per bounded turn instead of per prefix.
            while progress := callback(store.recommendations, checkpoint=False):
                result['recommendation_rows'] += progress.rows
                result['generations'] += progress.generations
                result['pools'] += progress.pools
        finally:
            if store._db_path is not None:
                checkpoint_wal(store._db_path)

    def group_cleanup():
        from yt_playlist.core import mix_choices
        mix_choices.collect_unpublished(store)
        reap_pending(store)
        while compact_group_publication.cleanup(store.taste_groups):
            result['group_steps'] += 1

    def group_walk():
        while group_retention.step(store.taste_groups):
            result['group_steps'] += 1

    def files(*, groups=False):
        attribute = '_group_output_collector' if groups else '_recommendation_output_collector'
        collector = getattr(store, attribute, None)
        if collector is None:
            with store._lock:
                directory = (group_documents if groups else recommendation_outputs).directory(store.conn)
            owners = tuple((table, prefix + '_filename') for table, _keys, prefix, _kind
                           in group_documents.OWNERS) if groups else None
            collector = recommendation_outputs.OrphanCollector(directory, owners=owners)
            setattr(store, attribute, collector)
        while True:
            with store._lock:
                progress = collector.step(store.conn)
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

    # Opening a private reader includes schema/repository admission. Leave time
    # for that fixed cost as well as one bounded durable traversal prefix.
    turns = [(group_cleanup, .25), (group_walk, .25), (lambda: files(groups=True), .1)]
    if recommendation_roots.enabled(store):
        turns = [(lambda: sql_turn(recommendation_publication.cleanup_generations), .1),
                 (lambda: sql_turn(recommendation_publication.cleanup_pools), .1), (files, .25), *turns]
    with store._lock:
        if not hasattr(store, '_retention_lock'):
            store._retention_lock = threading.Lock()
    if not store._retention_lock.acquire(blocking=False):
        result['pending'] = True
        return result
    try:
        for callback, seconds in turns:
            try:
                with work_budget(seconds):
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
