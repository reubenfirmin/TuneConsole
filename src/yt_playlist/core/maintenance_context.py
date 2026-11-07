"""Database access and disposable iterator state for existing file collectors."""


class MaintenanceDatabase:
    def __init__(self, connection, lock, path, paused):
        self._connection = connection
        self._lock = lock
        self._db_path = path
        self._paused = paused

    @property
    def conn(self):
        return self._connection()

    @property
    def activity_paused(self):
        return self._paused()
