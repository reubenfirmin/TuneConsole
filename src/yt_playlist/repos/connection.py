"""Connection-bound SQL adapters for preparation and recovery commands."""
from threading import RLock


class ConnectionRepo:
    """Reuse the serving owner's lock, or guard an exclusively owned connection.

    Plain SQLite connections belong to the calling offline command. Store
    connections carry their existing lock so adapters cannot enter another
    thread's transaction, including when several adapters use the same handle.
    """
    def __init__(self, conn):
        self.conn = conn
        lock = getattr(conn, '_repository_lock', None)
        self._lock = RLock() if lock is None else lock
