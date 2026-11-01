"""Read material for the monthly recap. No network, scoring, or presentation here."""
from datetime import datetime
from zoneinfo import ZoneInfo
from yt_playlist.core import genre_identity
from yt_playlist.repos.base import GENERATED_GROUP, Repo, synchronized


class RecapRepo(Repo):
    @synchronized
    def snapshot(self, since, until):
        """Bound detailed events to seven months; retain all earlier first-play evidence.

        Source spans include generated plays: these establish capture, never taste. Snapshots
        establish familiarity only; their appearances never become play counts.
        """
        quarantine = "COALESCE(is_generated,0)=0 AND NOT EXISTS (SELECT 1 FROM quarantined_playlists q " \
                     "JOIN playlist_scopes s ON s.id=q.playlist_scope_id WHERE q.name=? " \
                     "AND s.provider_playlist_id=pe.playlist_ytm_id " \
                     "AND (s.account_id IS NULL OR s.account_id=pe.account_id))"
        plays = [dict(r) for r in self.conn.execute(
            "SELECT pe.id,account_id, identity_key, video_id, played_at, playlist_ytm_id,c.day "
            "FROM play_event_subjects pe JOIN play_event_calendar c ON c.event_id=pe.id "
            f"WHERE c.day>=date(?,'unixepoch') AND c.day<date(?,'unixepoch') AND {quarantine} "
            "ORDER BY played_at, pe.id",
            (since, until, GENERATED_GROUP))]
        sources = {}
        for event_id,source in self.conn.execute('SELECT event_id,source FROM play_event_sources'):
            sources.setdefault(event_id,[]).append(source)
        for play in plays:
            play['sources'] = tuple(sorted(sources.get(play.pop('id'),())))
        first = {r[0]: r[1] for r in self.conn.execute(
            "SELECT identity_key, MIN(CAST(strftime('%s',c.day) AS REAL)) FROM play_event_subjects pe "
            "JOIN play_event_calendar c ON c.event_id=pe.id "
            f"WHERE c.day<date(?,'unixepoch') AND {quarantine} "
            "GROUP BY identity_key", (until, GENERATED_GROUP))}
        # A sync observation can disprove novelty, but cannot prove when a song was first heard.
        for r in self.conn.execute(
                "SELECT identity_key,MIN(day_ts) FROM listening_days WHERE day_ts<? "
                "GROUP BY identity_key", (until,)):
            first[r[0]] = min(first.get(r[0], r[1]), r[1])
        spans = {(r[0], r[1]): (r[2], r[3]) for r in self.conn.execute(
            "SELECT pe.account_id,s.source,MIN(CAST(strftime('%s',c.day) AS REAL)),"
            "MAX(CAST(strftime('%s',c.day) AS REAL)) FROM play_events pe "
            "JOIN play_event_calendar c ON c.event_id=pe.id "
            "JOIN play_event_sources s ON s.event_id=pe.id GROUP BY pe.account_id,s.source")}
        # Pick each metadata field independently, without multiplying events by video variants.
        meta = {}
        for r in self.conn.execute(
                "SELECT identity_key, title, artist, genre, mb_year FROM tracks ORDER BY id"):
            card = meta.setdefault(r["identity_key"], {})
            for field in ("title", "artist", "genre", "mb_year"):
                if r[field] and not card.get(field):
                    card[field] = r[field]
        for key, browse in self.conn.execute(
                f"SELECT DISTINCT t.identity_key,p.provider_id FROM tracks t JOIN {self.album_tracks_table} at ON at.entry_id=t.id "
                "JOIN album_primary_provider_ids p ON p.album_id=at.album_id AND p.provider='youtube'"):
            meta[key].setdefault('album_browse_ids',set()).add(browse)
        for card in meta.values():
            card['family'] = genre_identity.family(self.conn,card.get('genre'))
            releases = card.get('album_browse_ids',set())
            card['album_browse_id'] = next(iter(releases)) if len(releases)==1 else None
        artist_labels = dict(self.conn.execute('SELECT id,name FROM artists'))
        for key,aid in self.conn.execute('SELECT r.identity_key,a.artist_id FROM recording_listening_artists a '
                                         'JOIN recordings r ON r.id=a.recording_id ORDER BY a.artist_id'):
            if key in meta:
                meta[key].setdefault('artist_ids',[]).append(aid)
        familiar = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT a.artist_id FROM playlist_tracks pt JOIN catalog_entries t ON t.id=pt.track_id "
            "JOIN recording_listening_artists a ON a.recording_id=t.recording_id "
            "JOIN playlists p ON p.id=pt.playlist_id WHERE p.playlist_scope_id NOT IN "
            "(SELECT playlist_scope_id FROM quarantined_playlists WHERE name=?) UNION SELECT a.artist_id FROM account_albums m "
            "JOIN album_artists a ON a.album_id=m.album_id", (GENERATED_GROUP,))}
        albums = {}
        for row in self.conn.execute(
                "SELECT a.id,a.title,a.artist,a.thumbnail,p.provider_id browse_id, "
                f"COALESCE(r.identity_key,at.legacy_track_key) track_key FROM {self.album_fetch_state_table} f "
                "JOIN albums a ON a.id=f.album_id JOIN album_primary_provider_ids p ON p.album_id=a.id AND p.provider='youtube' "
                f"JOIN {self.album_tracks_table} at ON at.album_id=a.id AND at.is_full=1 "
                "LEFT JOIN catalog_entries e ON e.id=at.entry_id "
                "LEFT JOIN recordings r ON r.id=COALESCE(e.recording_id,at.recording_id) WHERE f.complete=1"):
            album = albums.setdefault(row['id'],{k:row[k] for k in ('title','artist','thumbnail','browse_id')})
            album.setdefault('keys',set()).add(row['track_key'])
        from yt_playlist.core import playlist_recipe_state
        recipe_scope = 'playlist_scope_id' if playlist_recipe_state.available(self.conn) else 'playlist_ytm'
        playlists = [dict(r) for r in self.conn.execute(
            "WITH candidates AS (SELECT provider_playlist_id,created_at,account_id,"
            "MIN(title) OVER (PARTITION BY provider_playlist_id) title,"
            "MAX(track_count) OVER (PARTITION BY provider_playlist_id) track_count,"
            "ROW_NUMBER() OVER (PARTITION BY provider_playlist_id ORDER BY created_at,id) first_created "
            "FROM playlists "
            "WHERE created_at>=? AND created_at<? AND created_at_source IN ('local','youtube') "
            "AND playlist_scope_id NOT IN (SELECT playlist_scope_id FROM quarantined_playlists WHERE name=?) "
            f"AND playlist_scope_id NOT IN (SELECT {recipe_scope} FROM rec_recipes) "
            ") SELECT provider_playlist_id,title,created_at,track_count,account_id "
            "FROM candidates WHERE first_created=1 ORDER BY created_at,provider_playlist_id",
            (since - 86400, until + 86400, GENERATED_GROUP))]
        calendars = dict(self.conn.execute("SELECT account_id,timezone FROM listening_calendars"))
        for playlist in playlists:
            zone = ZoneInfo(calendars.get(playlist.pop("account_id"), "UTC"))
            playlist["day"] = datetime.fromtimestamp(playlist["created_at"], zone).date().isoformat()
        return {"plays": plays, "first": first, "spans": spans, "meta": meta,
                "familiar_artists": familiar, "artist_labels":artist_labels,
                "albums": list(albums.values()), "playlists": playlists}
