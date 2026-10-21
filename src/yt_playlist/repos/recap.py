"""Read material for the monthly recap. No network, scoring, or presentation here."""
import json

from yt_playlist.repos.base import GENERATED_GROUP, Repo, synchronized


class RecapRepo(Repo):
    @synchronized
    def snapshot(self, since, until):
        """Bound detailed events to seven months; retain all earlier first-play evidence.

        Source spans include generated plays: these establish capture, never taste. Snapshots
        establish familiarity only; their appearances never become play counts.
        """
        quarantine = "COALESCE(is_generated,0)=0 AND (playlist_ytm_id IS NULL OR playlist_ytm_id NOT IN " \
                     "(SELECT ytm FROM quarantined_playlists WHERE name=?))"
        plays = [dict(r) for r in self.conn.execute(
            "SELECT identity_id, identity_key, video_id, played_at, source, playlist_ytm_id FROM play_events "
            f"WHERE played_at>=? AND played_at<? AND {quarantine} ORDER BY played_at, id",
            (since, until, GENERATED_GROUP))]
        first = {r[0]: r[1] for r in self.conn.execute(
            f"SELECT identity_key, MIN(played_at) FROM play_events WHERE played_at<? AND {quarantine} "
            "GROUP BY identity_key", (until, GENERATED_GROUP))}
        # A sync observation can disprove novelty, but cannot prove when a song was first heard.
        for r in self.conn.execute(
                "SELECT hi.identity_key, MIN(hs.taken_at) FROM history_items hi "
                "JOIN history_snapshots hs ON hs.id=hi.snapshot_id WHERE hs.taken_at<? "
                "GROUP BY hi.identity_key", (until,)):
            first[r[0]] = min(first.get(r[0], r[1]), r[1])
        spans = {(r[0], r[1]): (r[2], r[3]) for r in self.conn.execute(
            "SELECT identity_id, source, MIN(played_at), MAX(played_at) FROM play_events "
            "GROUP BY identity_id, source")}
        # Pick each metadata field independently, without multiplying events by video variants.
        meta = {}
        for r in self.conn.execute(
                "SELECT identity_key, title, artist, album_browse_id, genre, mb_year FROM tracks ORDER BY id"):
            card = meta.setdefault(r["identity_key"], {})
            for field in ("title", "artist", "album_browse_id", "genre", "mb_year"):
                if r[field] and not card.get(field):
                    card[field] = r[field]
        familiar = {r[0].casefold() for r in self.conn.execute(
            "SELECT DISTINCT t.artist FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "JOIN playlists p ON p.id=pt.playlist_id WHERE t.artist<>'' AND p.ytm_playlist_id NOT IN "
            "(SELECT ytm FROM quarantined_playlists WHERE name=?) UNION SELECT artist FROM saved_albums "
            "WHERE artist IS NOT NULL", (GENERATED_GROUP,))}
        albums = [{**dict(r), "keys": set(json.loads(r["track_keys"]))}
                  for r in self.conn.execute("SELECT * FROM album_tracklists")]
        playlists = [dict(r) for r in self.conn.execute(
            "SELECT ytm_playlist_id, MIN(title) title, MIN(created_at) created_at, "
            "MAX(track_count) track_count FROM playlists "
            "WHERE created_at>=? AND created_at<? AND created_at_source IN ('local','youtube') "
            "AND ytm_playlist_id NOT IN (SELECT ytm FROM quarantined_playlists WHERE name=?) "
            "AND ytm_playlist_id NOT IN (SELECT playlist_ytm FROM rec_recipes) "
            "GROUP BY ytm_playlist_id ORDER BY created_at, ytm_playlist_id", (since, until, GENERATED_GROUP))]
        return {"plays": plays, "first": first, "spans": spans, "meta": meta,
                "familiar_artists": familiar, "albums": albums, "playlists": playlists}
