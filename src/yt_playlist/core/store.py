"""Public application database assembly; SQLite owns no feature construction."""
from yt_playlist.core.sqlite_database import SQLiteDatabase
from yt_playlist.library.application_lifecycle import ApplicationLifecycle


class Store(SQLiteDatabase):
    def __init__(self, db_path):
        super().__init__(db_path, ApplicationLifecycle())

    @property
    def activity_paused(self):
        source = getattr(self, '_evidence_source', self)
        return (getattr(source, '_activity_paused', False)
                or getattr(source, '_recommendation_retirement_pending', False))

    @activity_paused.setter
    def activity_paused(self, value):
        self._activity_paused = value
