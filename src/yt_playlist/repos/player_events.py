"""#91 PlayerEventsRepo: the raw player/curation event stream. Append-only; every judgment about
these rows (skip vs completion, sessions) is a read-time derivation, never persisted here."""
from yt_playlist.library.listen_derive import classify_exit
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

    @synchronized
    def recent_skips_with_ts(self, since_ts) -> list[tuple[str, float]]:
        """#84 [(identity_key, at)] for skips, newest-first, deduped per key at its latest
        qualifying timestamp. Source rows are track_exit/bye events at/after since_ts with a
        video_id that resolves to a library identity_key (unresolved video_ids are dropped, same
        as identity_key_for_video); classify_exit (library.listen_derive, pure) decides "skip" at
        read time from the raw position/duration, so nothing about skip-ness is persisted here."""
        rows = self.conn.execute(
            "SELECT (SELECT identity_key FROM tracks WHERE video_id = pe.video_id LIMIT 1) AS key, "
            "pe.position AS position, pe.duration AS duration, pe.at AS at "
            "FROM player_events pe WHERE pe.kind IN ('track_exit', 'bye') AND pe.at >= ? "
            "ORDER BY pe.at DESC", (since_ts,)).fetchall()
        latest: dict[str, float] = {}
        for r in rows:
            key = r["key"]
            if key is None:
                continue
            if classify_exit(r["position"], r["duration"]) != "skip":
                continue
            latest.setdefault(key, float(r["at"]))  # rows are at DESC, so first hit is the latest
        return sorted(latest.items(), key=lambda kv: kv[1], reverse=True)
