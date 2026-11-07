"""Bounded calendar evidence queries and prepared day updates."""
from yt_playlist.core import calendar_lookup, listening_calendar
from yt_playlist.repos.base import Repo, snapshot_read, synchronized


class ListeningCalendarRepo(Repo):
    @snapshot_read
    def foreign_observations(self, account_id, *, observations=None, keys=None):
        return [dict(row) for row in listening_calendar.foreign_rows(
            self.conn, account_id, observations=observations, keys=keys)]

    @snapshot_read
    def exact_days(self, account_id, key, start, end):
        _, events, account = listening_calendar._subjects(self.conn)
        if events == 'play_event_subjects':
            indexed = calendar_lookup.selected(self.conn)
            unresolved = ('FROM ' + calendar_lookup.EVENTS + ' lookup JOIN play_events pe ON pe.id=lookup.id '
                'JOIN play_event_calendar c ON c.event_id=pe.id WHERE lookup.account_id=? '
                'AND lookup.unresolved_subject_key=? AND lookup.played_at>=? AND lookup.played_at<?') if indexed else (
                'FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id WHERE pe.account_id=? '
                'AND pe.unresolved_subject_key=? AND pe.played_at>=? AND pe.played_at<?')
            query = ('SELECT pe.played_at,c.day FROM play_events pe '
                'JOIN play_event_calendar c ON c.event_id=pe.id WHERE pe.account_id=? '
                'AND pe.recording_id=(SELECT id FROM recordings WHERE identity_key=?) '
                'AND pe.played_at>=? AND pe.played_at<? UNION ALL SELECT pe.played_at,c.day ' + unresolved)
            parameters = (account_id, key, start, end) * 2
        else:
            query = (f'SELECT pe.played_at,c.day FROM {events} pe '
                'JOIN play_event_calendar c ON c.event_id=pe.id '
                f'WHERE pe.{account}=? AND pe.identity_key=? AND pe.played_at>=? AND pe.played_at<?')
            parameters = (account_id, key, start, end)
        return [dict(row) for row in self.conn.execute(query, parameters)]

    @snapshot_read
    def recording_scopes(self, recordings):
        if not recordings:
            return []
        observations, _, account = listening_calendar._subjects(self.conn)
        marks = ','.join('?' for _ in recordings)
        return [dict(row) for row in self.conn.execute(
            f'SELECT o.{account} account_id,o.identity_key,o.source_day,o.timezone '
            f'FROM {observations} o JOIN listening_calendars c ON c.{account}=o.{account} '
            f'WHERE o.recording_id IN ({marks}) '
            "AND o.precision='calendar_day' AND o.timezone<>c.timezone", recordings)]

    @synchronized
    def store_days(self, updates):
        with self.conn.atomic(join=True):
            self.conn.executemany('UPDATE main.listening_observations SET day=? WHERE id=?', updates)
