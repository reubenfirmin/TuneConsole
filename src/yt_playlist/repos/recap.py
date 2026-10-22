"""Read material for the monthly recap. No network, scoring, or presentation here."""
from yt_playlist.repos.base import GENERATED_GROUP, Repo, synchronized


class RecapRepo(Repo):
    @synchronized
    def snapshot(self, since, until):
        """Bound detailed events to seven months; retain all earlier first-play evidence.

        Source spans include generated plays: these establish capture, never taste. Snapshots
        establish familiarity only; their appearances never become play counts.
        """
        quarantine = "COALESCE(is_generated,0)=0 AND NOT EXISTS (SELECT 1 FROM quarantined_playlists q " \
                     "JOIN playlist_scopes s ON s.ref=q.ytm WHERE q.name=? " \
                     "AND s.provider_id=play_events.playlist_ytm_id " \
                     "AND (s.identity_id IS NULL OR s.identity_id=play_events.identity_id))"
        plays = [dict(r) for r in self.conn.execute(
            "SELECT identity_id, identity_key, video_id, played_at, source, playlist_ytm_id FROM play_events "
            f"WHERE played_at>=? AND played_at<? AND {quarantine} ORDER BY played_at, id",
            (since, until, GENERATED_GROUP))]
        first = {r[0]: r[1] for r in self.conn.execute(
            f"SELECT identity_key, MIN(played_at) FROM play_events WHERE played_at<? AND {quarantine} "
            "GROUP BY identity_key", (until, GENERATED_GROUP))}
        # A sync observation can disprove novelty, but cannot prove when a song was first heard.
        for r in self.conn.execute(
                "SELECT identity_key,MIN(day_ts) FROM listening_days WHERE day_ts<? "
                "GROUP BY identity_key", (until,)):
            first[r[0]] = min(first.get(r[0], r[1]), r[1])
        spans = {(r[0], r[1]): (r[2], r[3]) for r in self.conn.execute(
            "SELECT pe.identity_id,s.source,MIN(pe.played_at),MAX(pe.played_at) FROM play_events pe "
            "JOIN play_event_sources s ON s.event_id=pe.id GROUP BY pe.identity_id,s.source")}
        # Pick each metadata field independently, without multiplying events by video variants.
        meta = {}
        for r in self.conn.execute(
                "SELECT identity_key, title, artist, genre, mb_year FROM tracks ORDER BY id"):
            card = meta.setdefault(r["identity_key"], {})
            for field in ("title", "artist", "genre", "mb_year"):
                if r[field] and not card.get(field):
                    card[field] = r[field]
        for key, browse in self.conn.execute(
                "SELECT DISTINCT t.identity_key,p.provider_id FROM tracks t JOIN album_tracks at ON at.entry_id=t.id "
                "JOIN album_provider_ids p ON p.album_id=at.album_id AND p.provider='youtube'"):
            meta[key].setdefault('album_browse_ids',set()).add(browse)
        for card in meta.values():
            releases = card.get('album_browse_ids',set())
            card['album_browse_id'] = next(iter(releases)) if len(releases)==1 else None
        familiar = {r[0].casefold() for r in self.conn.execute(
            "SELECT DISTINCT t.artist FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "JOIN playlists p ON p.id=pt.playlist_id WHERE t.artist<>'' AND p.scope_ref NOT IN "
            "(SELECT ytm FROM quarantined_playlists WHERE name=?) UNION SELECT artist FROM saved_albums "
            "WHERE artist IS NOT NULL", (GENERATED_GROUP,))}
        albums = {}
        for row in self.conn.execute(
                "SELECT a.id,a.title,a.artist,a.thumbnail,p.provider_id browse_id, "
                "COALESCE(r.identity_key,at.legacy_track_key) track_key FROM album_fetch_state f "
                "JOIN albums a ON a.id=f.album_id JOIN album_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
                "JOIN album_tracks at ON at.album_id=a.id AND at.is_full=1 "
                "LEFT JOIN catalog_entries e ON e.id=at.entry_id "
                "LEFT JOIN recordings r ON r.id=COALESCE(e.recording_id,at.recording_id) WHERE f.complete=1"):
            album = albums.setdefault(row['id'],{k:row[k] for k in ('title','artist','thumbnail','browse_id')})
            album.setdefault('keys',set()).add(row['track_key'])
        playlists = [dict(r) for r in self.conn.execute(
            "SELECT ytm_playlist_id, MIN(title) title, MIN(created_at) created_at, "
            "MAX(track_count) track_count FROM playlists "
            "WHERE created_at>=? AND created_at<? AND created_at_source IN ('local','youtube') "
            "AND scope_ref NOT IN (SELECT ytm FROM quarantined_playlists WHERE name=?) "
            "AND scope_ref NOT IN (SELECT playlist_ytm FROM rec_recipes) "
            "GROUP BY ytm_playlist_id ORDER BY created_at, ytm_playlist_id", (since, until, GENERATED_GROUP))]
        return {"plays": plays, "first": first, "spans": spans, "meta": meta,
                "familiar_artists": familiar, "albums": list(albums.values()), "playlists": playlists}
