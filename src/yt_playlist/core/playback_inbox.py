"""Compatibility entry points for the separately composed playback inbox."""
from yt_playlist.core.playback_observation_files import ObservationFiles
from yt_playlist.repos.playback_inbox import owns_file as _owns_file


def Inbox(database, memory_key):
    from yt_playlist.library.playback_inbox_composition import inbox_for_database
    return inbox_for_database(database, memory_key)


Inbox.body = ObservationFiles.body


def inbox(store):
    return getattr(store, '_evidence_source', store)._playback_feedback


def deferred(store):
    return bool(store.activity_paused or store.playback_feedback.pending())


def capture(store, kind, identity, body, now):
    return store.playback_feedback.capture(kind, identity, body, now)


def _queue(conn):
    from pathlib import Path
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return Inbox(Path(path).resolve() if path else None, getattr(conn, '_policy_memory_key', str(id(conn))))


def directory(conn):
    return _queue(conn).directory


def owns_file(conn, filename):
    return _owns_file(_queue(conn).path, filename)
