"""#91 PlayerEventsRepo: the raw player/curation event stream. Append-only; every judgment about
these rows (skip vs completion, sessions) is a read-time derivation, never persisted here."""
from yt_playlist.repos.base import Repo, synchronized


class PlayerEventsRepo(Repo):
    @synchronized
    def record_player_event(self, identity_id, kind, video_id, position, duration,
                            playlist_ytm_id, payload, at) -> int:
        cur = self.conn.execute(
            "INSERT INTO player_events(identity_id, kind, video_id, position, duration, "
            "playlist_ytm_id, payload, at) VALUES (?,?,?,?,?,?,?,?)",
            (identity_id, kind, video_id, position, duration, playlist_ytm_id, payload, at))
        self.conn.commit()
        return cur.lastrowid

    @synchronized
    def player_events_since(self, since_ts, kind=None) -> list[dict]:
        """Events at/after since_ts, oldest first; optionally one kind."""
        q = ("SELECT identity_id, kind, video_id, position, duration, playlist_ytm_id, payload, at "
             "FROM player_events WHERE at>=?")
        args = [since_ts]
        if kind is not None:
            q += " AND kind=?"
            args.append(kind)
        rows = self.conn.execute(q + " ORDER BY at, id", args).fetchall()
        return [dict(r) for r in rows]
