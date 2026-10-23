"""Bounded scorecard inputs and historical ownership on a private read snapshot."""
from bisect import bisect_right
from collections import defaultdict

from yt_playlist.core.activity import check_cancellable_work


LIVE = "COALESCE(json_extract(provenance,'$.policy_kind'),'live')='live'"
VIDEO = "json_extract(provenance,'$.candidate.video_id')"


def _chunks(values):
    values = sorted(values)
    for offset in range(0, len(values), 400):
        check_cancellable_work()
        chunk = values[offset:offset + 400]
        yield chunk, ','.join('?' for _ in chunk)


class _Timeline:
    def __init__(self, rows, key, value, horizon):
        grouped = defaultdict(list)
        for row in rows:
            grouped[row[key]].append((row['served_at'], row['id'], row[value]))
        self.groups = {}
        self.horizon = horizon
        for name, entries in grouped.items():
            entries.sort(key=lambda entry: (entry[0], entry[1]))
            self.groups[name] = ([entry[0] for entry in entries], [entry[2] for entry in entries])

    def nearest(self, key, at):
        timeline = self.groups.get(key)
        if timeline is None:
            return None
        times, values = timeline
        index = bisect_right(times, at) - 1
        return values[index] if index >= 0 and times[index] >= at - self.horizon else None


class Attribution:
    def __init__(self, conn, keys, start, end, horizon):
        self.plays, self.responses, self.exits = [], [], []
        self.fallback = {}
        owners = []
        # A report with no qualifying live impressions needs no event scans,
        # catalog joins, JSON video lookups or attribution work.
        for chunk, placeholders in _chunks(keys):
            owners.extend(conn.execute(
                'SELECT id,identity_key,served_at FROM rec_served_impressions '
                f'WHERE identity_key IN ({placeholders}) AND {LIVE} '
                'AND served_at>=? AND served_at<=?', (*chunk, start - horizon, end + horizon)))
            self.plays.extend(conn.execute(
                'SELECT identity_key,played_at,is_generated FROM play_events '
                f'WHERE identity_key IN ({placeholders}) AND played_at>=? AND played_at<=?',
                (*chunk, start, end + horizon)))
            self.responses.extend(conn.execute(
                'SELECT identity_key,at,kind,is_generated FROM rec_response_events '
                f'WHERE identity_key IN ({placeholders}) AND at>=? AND at<=?',
                (*chunk, start, end + horizon)))
        self.owners = _Timeline(owners, 'identity_key', 'id', horizon)
        videos = []
        if keys:
            self.exits = conn.execute(
                'SELECT video_id,position,duration,at FROM player_events '
                "WHERE kind IN ('track_exit','bye','ended') AND at>=? AND at<=?",
                (start, end + horizon)).fetchall()
            # Resolve against all live snapshots for these videos, including
            # owners outside the report and identities later edited/removed.
            for chunk, placeholders in _chunks({r['video_id'] for r in self.exits if r['video_id']}):
                videos.extend(conn.execute(
                    f'SELECT id,identity_key,served_at,{VIDEO} video_id FROM rec_served_impressions '
                    f'WHERE {VIDEO} IN ({placeholders}) AND {LIVE} '
                    'AND served_at>=? AND served_at<=?', (*chunk, start - horizon, end + horizon)))
        self.video_owners = _Timeline(videos, 'video_id', 'identity_key', horizon)
        missing = {r['video_id'] for r in self.exits if r['video_id']
                   and self.video_owners.nearest(r['video_id'], r['at']) is None}
        # Legacy impressions lack a video snapshot. Preserve catalog precedence
        # with one bounded bulk lookup per set of distinct unresolved videos.
        for table in ('tracks', 'discovered_tracks'):
            for chunk, placeholders in _chunks(missing - self.fallback.keys()):
                for row in conn.execute(f'SELECT video_id,identity_key FROM {table} '
                                        f'WHERE video_id IN ({placeholders})', chunk):
                    if row['identity_key'] is not None:
                        self.fallback.setdefault(row['video_id'], row['identity_key'])

    def nearest(self, key, at):
        return self.owners.nearest(key, at)

    def exit_key(self, video, at):
        historical = self.video_owners.nearest(video, at)
        return historical if historical is not None else self.fallback.get(video)
