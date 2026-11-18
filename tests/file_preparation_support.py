"""Exercise retained current-format conversion owners without retired CLI wrappers."""
from types import SimpleNamespace
from yt_playlist.core import profile_files, embedding_files, vibe_provenance
from yt_playlist.core.db_writes import atomic_write


def prepare_profiles(store, *, limit=128):
    count = 0
    for kind in profile_files.KINDS:
        with atomic_write(store):
            count += profile_files.prepare_batch(store.conn, kind, limit)
    return count


def prepare_embeddings(store, *, limit=128):
    count = 0
    for kind in ('recording', 'artist'):
        with atomic_write(store):
            count += embedding_files.prepare_batch(store.conn, kind, limit)
    return count


def prepare_provenance(store, *, limit=128):
    with atomic_write(store):
        return vibe_provenance.prepare_batch(store.conn, limit)


profiles = SimpleNamespace(prepare=prepare_profiles)
embeddings = SimpleNamespace(prepare=prepare_embeddings)
provenance = SimpleNamespace(prepare=prepare_provenance)
policy_history = SimpleNamespace(prepare=lambda store, *, now, limit=128:
    store.rec_ledger.prepare_policy_history(now, limit=limit))
