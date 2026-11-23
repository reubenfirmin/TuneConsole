"""Database access and disposable iterator state for existing file collectors."""


class MaintenanceDatabase:
    def __init__(self, connection, lock, path, paused):
        self.conn = connection
        self._lock = lock
        self._db_path = path
        self._paused = paused

    @property
    def activity_paused(self):
        return self._paused()
