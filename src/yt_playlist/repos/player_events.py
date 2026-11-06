"""#91 PlayerEventsRepo: the raw player/curation event stream. Append-only; every judgment about
these rows (skip vs completion, sessions) is a read-time derivation, never persisted here."""
from yt_playlist.core.db_revisions import keyed_reads
from yt_playlist.core import entity_names
from yt_playlist.repos.base import Repo, synchronized


class PlayerEventsRepo(Repo):
    @synchronized
    def record_player_event(self, account_id, kind, video_id, position, duration,
                            playlist_ytm_id, payload, at) -> int:
        events = entity_names.owner(self.conn, 'player_events')
        cur = self.conn.execute(
            f'INSERT INTO {events.table}({events.column("account_id")}, kind, video_id, position, duration, '
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
    def library_exits(self, since_ts):
        """Raw exit facts with library identity, newest first."""
        with keyed_reads(self._store, 'player_events', ('track_exit', 'bye')):
            rows = self.conn.execute(
                "SELECT (SELECT identity_key FROM tracks WHERE video_id = pe.video_id LIMIT 1) AS key, "
                "pe.position AS position, pe.duration AS duration, pe.at AS at "
                "FROM player_events pe WHERE pe.kind IN ('track_exit', 'bye') AND pe.at >= ? "
                "ORDER BY pe.at DESC", (since_ts,)).fetchall()
        return [dict(row) for row in rows]

    @synchronized
    def recording_exits(self, since_ts, until_ts, *, keys=None):
        """Raw upload-resolved exits within supplied bounds, oldest first."""
        if keys == []:
            return []
        clause = (' AND r.identity_key IN (' + ','.join('?' for _ in keys) + ')') if keys is not None else ''
        with keyed_reads(self._store, 'player_events', ('track_exit', 'bye', 'next')):
            rows = self.conn.execute(
                'SELECT r.identity_key AS key, pe.account_id, pe.kind, pe.position, pe.duration, pe.at '
                'FROM player_events pe JOIN youtube_items y ON y.video_id=pe.video_id '
                'JOIN recordings r ON r.id=y.recording_id '
                "WHERE pe.kind IN ('track_exit','bye','next') AND pe.at>=? AND pe.at<=?" + clause +
                ' ORDER BY pe.at,pe.id',
                (since_ts, until_ts, *(keys or []))).fetchall()
        return [dict(row) for row in rows]
