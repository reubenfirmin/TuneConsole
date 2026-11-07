"""Bounded detached scorecard timelines, with no report transaction."""
from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core import served_facts
from yt_playlist.repos.base import Repo, synchronized


def _chunks(values):
    values = sorted(values)
    for offset in range(0, len(values), 400):
        check_cancellable_work()
        chunk = values[offset:offset + 400]
        yield chunk, ','.join('?' for _ in chunk)


class ScorecardFacts(Repo):
    @synchronized
    def owners(self, keys, since, until, *, policy_kind):
        live = served_facts.expression(self.conn, 'policy_kind', "COALESCE(json_extract(provenance,'$.policy_kind'),'live')")
        rows = []
        for chunk, placeholders in _chunks(keys):
            rows.extend(dict(row) for row in self.conn.execute(
                'SELECT id,identity_key,served_at FROM rec_served_impressions '
                f'WHERE identity_key IN ({placeholders}) AND {live}=? '
                'AND served_at>=? AND served_at<=?', (*chunk, policy_kind, since, until)))
        return rows

    @synchronized
    def outcomes(self, keys, since, until):
        plays, responses = [], []
        for chunk, placeholders in _chunks(keys):
            plays.extend(dict(row) for row in self.conn.execute(
                'SELECT identity_key,played_at,is_generated FROM play_event_subjects '
                f'WHERE identity_key IN ({placeholders}) AND played_at>=? AND played_at<=?',
                (*chunk, since, until)))
            responses.extend(dict(row) for row in self.conn.execute(
                'SELECT identity_key,at,kind,is_generated FROM rec_response_events '
                f'WHERE identity_key IN ({placeholders}) AND at>=? AND at<=?',
                (*chunk, since, until)))
        return plays, responses

    @synchronized
    def exits(self, since, until, *, kinds):
        placeholders = ','.join('?' for _ in kinds)
        return [dict(row) for row in self.conn.execute(
            'SELECT video_id,position,duration,at FROM player_events '
            f'WHERE kind IN ({placeholders}) AND at>=? AND at<=?', (*kinds, since, until))]

    @synchronized
    def video_owners(self, videos, since, until, *, policy_kind):
        live = served_facts.expression(self.conn, 'policy_kind', "COALESCE(json_extract(provenance,'$.policy_kind'),'live')")
        video = served_facts.expression(self.conn, 'video_id', "json_extract(provenance,'$.candidate.video_id')")
        rows = []
        for chunk, placeholders in _chunks(videos):
            rows.extend(dict(row) for row in self.conn.execute(
                f'SELECT id,identity_key,served_at,{video} video_id FROM rec_served_impressions '
                f'WHERE {video} IN ({placeholders}) AND {live}=? '
                'AND served_at>=? AND served_at<=?', (*chunk, policy_kind, since, until)))
        return rows

    @synchronized
    def catalog_keys(self, videos, *, discovered):
        table = 'discovered_tracks' if discovered else 'tracks'
        rows = []
        for chunk, placeholders in _chunks(videos):
            rows.extend(dict(row) for row in self.conn.execute(
                f'SELECT video_id,identity_key FROM {table} WHERE video_id IN ({placeholders})', chunk))
        return rows
