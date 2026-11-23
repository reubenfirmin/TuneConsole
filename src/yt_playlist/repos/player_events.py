"""#91 PlayerEventsRepo: the raw player/curation event stream. Append-only; every judgment about
these rows (skip vs completion, sessions) is a read-time derivation, never persisted here."""
import json

from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.repos.base import Repo, synchronized


_PLAYBACK_ACTIVITY = "(kind='bye' OR (video_id IS NOT NULL AND kind IN ('tick','state')))"


# The next command and the browser exit are separate observations of one
# playback. Keep both in the event log and exclude them from skip inference.
_RATING_ADVANCE_OF_EXIT = """
    EXISTS (
        SELECT 1 FROM player_events advance
        WHERE advance.kind = 'next' AND advance.at BETWEEN pe.at - 45 AND pe.at
            AND advance.account_id = pe.account_id AND advance.video_id = pe.video_id
            AND advance.playlist_ytm_id IS pe.playlist_ytm_id
            AND CASE WHEN json_valid(advance.payload)
                THEN json_extract(advance.payload, '$.rating_advance') = 1 ELSE 0 END
            AND (advance.position IS NULL OR pe.position IS NULL
                OR abs((pe.at - pe.position) - (advance.at - advance.position)) <= 3
                OR (pe.at - advance.at <= 5 AND abs(pe.position - advance.position) <= 5))
    )
"""
_LISTENING_EXIT = f"""
    AND CASE WHEN json_valid(pe.payload)
        THEN json_extract(pe.payload, '$.rating_advance') IS NOT 1 ELSE 1 END
    AND (pe.kind = 'next' OR NOT {_RATING_ADVANCE_OF_EXIT})
"""


class PlayerEventsRepo(Repo):
    @synchronized
    def event_for_capture(self, account_id, at, capture_id):
        row = self.conn.execute(
            'SELECT id FROM player_events WHERE account_id=? AND at=? '
            "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.capture_id') END=? LIMIT 1",
            (account_id, at, capture_id)).fetchone()
        return row[0] if row is not None else None

    @synchronized
    def record_player_event(self, account_id, kind, video_id, position, duration,
                            playlist_ytm_id, payload, at, *, capture_id=None) -> int:
        if capture_id is not None:
            existing = self.event_for_capture(account_id, at, capture_id)
            if existing is not None:
                return existing
        cur = self.conn.execute(
            'INSERT INTO player_events(account_id, kind, video_id, position, duration, '
            "playlist_ytm_id, payload, at) VALUES (?,?,?,?,?,?,?,?)",
            (account_id, kind, video_id, position, duration, playlist_ytm_id, payload, at))
        self.conn.commit()
        return cur.lastrowid

    @synchronized
    def player_events_since(self, since_ts, kind=None) -> list[dict]:
        """Events at/after since_ts, oldest first; optionally one kind."""
        q = ("SELECT account_id, kind, video_id, position, duration, playlist_ytm_id, payload, at "
             "FROM player_events WHERE at>=?")
        args = [since_ts]
        if kind is not None:
            q += " AND kind=?"
            args.append(kind)
        rows = self.conn.execute(q + " ORDER BY at, id", args).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def playback_activity(self, since, until):
        # Walk the requested time range in order. The kind indexes can scan
        # the full history for each event kind, then sort the matches.
        with keyed_reads(self._store, 'player_events', ('tick', 'state', 'bye')):
            return [dict(row) for row in self.conn.execute(
                'SELECT account_id,at,kind,payload FROM player_events INDEXED BY ix_player_event_recent '
                f'WHERE at BETWEEN ? AND ? AND {_PLAYBACK_ACTIVITY} ORDER BY at,id', (since, until))]

    @synchronized
    def latest_id(self):
        return self.conn.execute('SELECT COALESCE(MAX(id),0) FROM player_events').fetchone()[0]

    @synchronized
    def activity_added_before(self, after_id, through):
        return self.conn.execute(
            f'SELECT 1 FROM player_events WHERE id>? AND at<=? AND {_PLAYBACK_ACTIVITY} LIMIT 1',
            (after_id, through)).fetchone() is not None

    @synchronized
    def library_exits(self, since_ts):
        """Listening exits with library identity, newest first."""
        with keyed_reads(self._store, 'player_events', ('track_exit', 'bye', 'next')):
            rows = self.conn.execute(
                "SELECT (SELECT identity_key FROM catalog_tracks WHERE video_id = pe.video_id LIMIT 1) AS key, "
                "pe.position AS position, pe.duration AS duration, pe.at AS at "
                "FROM player_events pe WHERE pe.kind IN ('track_exit', 'bye') AND pe.at >= ? " + _LISTENING_EXIT +
                "ORDER BY pe.at DESC", (since_ts,)).fetchall()
        return [dict(row) for row in rows]

    @synchronized
    def listening_exits(self, since_ts):
        with keyed_reads(self._store, 'player_events', ('track_exit', 'next')):
            return [dict(row) for row in self.conn.execute(
                "SELECT pe.* FROM player_events pe WHERE pe.kind='track_exit' AND pe.at>=? "
                + _LISTENING_EXIT + ' ORDER BY pe.at,pe.id', (since_ts,))]

    @synchronized
    def rating_advance_exit_ids(self, event_ids):
        if not event_ids:
            return set()
        with keyed_reads(self._store, 'player_events', ('track_exit', 'next')):
            return {row[0] for row in self.conn.execute(
                "SELECT pe.id FROM player_events pe WHERE pe.id IN (SELECT value FROM json_each(?)) "
                "AND pe.kind='track_exit' AND " + _RATING_ADVANCE_OF_EXIT, (json.dumps(list(event_ids)),))}

    @synchronized
    def recording_exits(self, since_ts, until_ts, *, keys=None):
        """Upload-resolved listening exits within supplied bounds, oldest first."""
        if keys == []:
            return []
        clause = (' AND r.identity_key IN (' + ','.join('?' for _ in keys) + ')') if keys is not None else ''
        with keyed_reads(self._store, 'player_events', ('track_exit', 'bye', 'next')):
            rows = self.conn.execute(
                'SELECT r.identity_key AS key, pe.account_id, pe.kind, pe.position, pe.duration, pe.at '
                'FROM player_events pe JOIN track_sources y ON y.video_id=pe.video_id '
                'JOIN recordings r ON r.id=y.recording_id '
                "WHERE pe.kind IN ('track_exit','bye','next') AND pe.at>=? AND pe.at<=?" + clause + _LISTENING_EXIT +
                ' ORDER BY pe.at,pe.id',
                (since_ts, until_ts, *(keys or []))).fetchall()
        return [dict(row) for row in rows]
