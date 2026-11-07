"""Choose listening-day projections from bounded observations and exact events."""
import datetime as dt
from collections import defaultdict

from yt_playlist.core.listening_calendar import day_at, zone


class ListeningCalendar:
    def __init__(self, repository):
        self.repository = repository

    def reconcile(self, account_id, *, observations=None, events=None):
        by_key = defaultdict(set)
        for key, timestamp in events or ():
            by_key[key].add(timestamp)
        affected_events = events is not None and observations is None
        rows = self.repository.foreign_observations(account_id, observations=observations,
            keys=set(by_key) if affected_events else None)
        source_days, projected, bounds, updates = {}, {}, {}, []
        for row in rows:
            key, source_day, timezone = row['identity_key'], row['source_day'], row['timezone']
            if affected_events:
                pair = key, timezone
                if pair not in source_days:
                    source_days[pair] = {day_at(at, timezone) for at in by_key[key]}
                if source_day not in source_days[pair]:
                    continue
            scope = key, source_day, timezone
            if scope not in projected:
                date_zone = source_day, timezone
                if date_zone not in bounds:
                    date, tz = dt.date.fromisoformat(source_day), zone(timezone)
                    bounds[date_zone] = (
                        dt.datetime.combine(date, dt.time(), tz).timestamp(),
                        dt.datetime.combine(date + dt.timedelta(days=1), dt.time(), tz).timestamp())
                candidates = self.repository.exact_days(account_id, key, *bounds[date_zone])
                matches = {event['day'] for event in candidates if day_at(event['played_at'], timezone) == source_day}
                projected[scope] = next(iter(matches)) if len(matches) == 1 else None
            if projected[scope] != row['day']:
                updates.append((projected[scope], row['id']))
        if updates:
            self.repository.store_days(updates)

    def reconcile_recordings(self, recordings):
        scopes = defaultdict(set)
        for row in self.repository.recording_scopes(sorted(set(recordings))):
            scopes[row['account_id']].add((row['identity_key'], row['source_day'], row['timezone']))
        for account_id, observations in scopes.items():
            self.reconcile(account_id, observations=observations)
