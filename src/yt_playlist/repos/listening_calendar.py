"""Bounded calendar evidence queries and prepared day updates."""
from yt_playlist.core import listening_calendar
from yt_playlist.repos.base import Repo, snapshot_read, synchronized


class ListeningCalendarRepo(Repo):
    @snapshot_read
    def foreign_observations(self, account_id, *, observations=None, keys=None):
        return [dict(row) for row in listening_calendar.foreign_rows(
            self.conn, account_id, observations=observations, keys=keys)]

    @snapshot_read
    def exact_days(self, account_id, key, start, end):
        return [dict(row) for row in self.conn.execute(listening_calendar.EXACT_DAYS, (account_id, key, start, end) * 2)]

    @snapshot_read
    def recording_scopes(self, recordings):
        if not recordings:
            return []
        marks = ','.join('?' for _ in recordings)
        return [dict(row) for row in self.conn.execute(
            'SELECT o.account_id,o.identity_key,o.source_day,o.timezone FROM listening_observation_subjects o '
            'JOIN accounts c ON c.id=o.account_id '
            f'WHERE o.recording_id IN ({marks}) '
            "AND o.precision='calendar_day' AND o.timezone<>c.listening_timezone", recordings)]

    @synchronized
    def store_days(self, updates):
        with self.conn.atomic(join=True):
            self.conn.executemany('UPDATE main.listening_observations SET day=? WHERE id=?', updates)
