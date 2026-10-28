"""Historical recommendation evidence and reconstructable training baskets.

Cutoffs are applied here, alongside the provenance needed to keep generated plays
out of held-out evaluation. Basket reads pin one snapshot for all eligibility and
membership queries, including calls into the shared eligibility owner.
"""
from yt_playlist.repos.base import GENERATED_GROUP, Repo, synchronized, snapshot_read
from yt_playlist.repos.rec_eligibility import exclude_ids
from yt_playlist.util import genre_map
from yt_playlist.util.years import decade

_ARTIST_BASKET_CAP = 50
_CONTENT_BASKET_CAP = 80


class RecEvidenceRepo(Repo):
    @synchronized
    def generated_only_play_days(self, group=GENERATED_GROUP) -> set[tuple[int, str]]:
        """Day/key pairs with generated events and no account's independent evidence."""
        rows = self.conn.execute(
            "SELECT pe.identity_key k,CAST(julianday(c.day)-julianday('1970-01-01') AS INTEGER) day,"
            "SUM(CASE WHEN pe.is_generated=1 THEN 0 ELSE 1 END) non_generated "
            "FROM play_events pe JOIN play_event_calendar c ON c.event_id=pe.id "
            "GROUP BY pe.identity_key,c.day HAVING NOT EXISTS "
            "(SELECT 1 FROM listening_days d WHERE d.identity_key=pe.identity_key AND d.day=c.day)").fetchall()
        return {(r["day"], r["k"]) for r in rows if r["non_generated"] == 0}

    @synchronized
    def evaluation_history_bounds(self) -> tuple:
        """Include generated events in the clock, so exclusions cannot move holdout."""
        row = self.conn.execute(
            "SELECT MIN(at),MAX(at) FROM (SELECT at FROM listening_activity "
            "UNION ALL SELECT played_at FROM play_events WHERE is_generated=1)").fetchone()
        return tuple(row)

    @synchronized
    def generated_only_keys_since(self, since_ts, group=GENERATED_GROUP) -> set[str]:
        """Keys with only generated event provenance at/after the inclusive cutoff.

        Day-model observations have no generated provenance and conservatively
        prevent exclusion. Keys with no events in the window are likewise kept.
        """
        rows = self.conn.execute(
            "SELECT pe.identity_key k,SUM(CASE WHEN pe.is_generated=1 THEN 0 ELSE 1 END) non_generated "
            "FROM play_events pe WHERE pe.played_at>=? GROUP BY pe.identity_key HAVING NOT EXISTS "
            "(SELECT 1 FROM listening_days d WHERE d.identity_key=pe.identity_key AND d.day_ts>=?)",
            (since_ts,since_ts)).fetchall()
        return {r["k"] for r in rows if r["non_generated"] == 0}

    @synchronized
    def generated_only_keys_between(self, start_ts, end_ts, group=GENERATED_GROUP) -> set[str]:
        """Generated-only provenance for causal evaluation's [start, end) interval."""
        rows = self.conn.execute(
            "SELECT pe.identity_key k,SUM(CASE WHEN pe.is_generated=1 THEN 0 ELSE 1 END) non_generated "
            "FROM play_events pe WHERE pe.played_at>=? AND pe.played_at<? GROUP BY pe.identity_key "
            "HAVING NOT EXISTS (SELECT 1 FROM listening_days d WHERE d.identity_key=pe.identity_key "
            "AND d.day_ts>=? AND d.day_ts<?)",(start_ts,end_ts,start_ts,end_ts)).fetchall()
        return {r["k"] for r in rows if r["non_generated"] == 0}

    @synchronized
    def plays_by_list_ids_since(self, ytm_ids, since_ts) -> set[str]:
        """Radio-session freshness by exact playlist ID, independently of quarantine."""
        ids = {y for y in ytm_ids if y}
        if not ids:
            return set()
        qs = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT DISTINCT identity_key k FROM play_events WHERE playlist_ytm_id IN ({qs}) "
            "AND played_at >= ?", list(ids) + [since_ts]).fetchall()
        return {r["k"] for r in rows}

    @snapshot_read
    def rec_baskets(self, max_album=30, max_session=120) -> list[list[str]]:
        """Eligible playlist, album, artist, listening-session and content baskets.

        Generated-only songs never contribute. Overlarge catch-all playlists and
        over-cap album/artist/content/session baskets carry too little information.
        """
        eligibility = self._store.rec_eligibility
        good = {r["k"] for r in self.conn.execute(
            "SELECT DISTINCT identity_key k FROM tracks "
            "WHERE (video_type IS NULL OR video_type <> 'MUSIC_VIDEO_TYPE_UGC') "
            f"AND {eligibility.track_length_clause()}")}
        good -= eligibility.generated_only_keys()
        excl = eligibility.excluded_playlist_ids() | eligibility.catchall_playlist_ids()
        pl_where = exclude_ids("pt.playlist_id", excl, connective="WHERE")
        out = []
        pbuckets: dict = {}
        for r in self.conn.execute(
                "SELECT pt.playlist_id g, t.identity_key k FROM playlist_tracks pt "
                "JOIN tracks t ON t.id=pt.track_id" + pl_where):
            if r["k"] in good:
                pbuckets.setdefault(r["g"], set()).add(r["k"])
        out += [list(s) for s in pbuckets.values() if len(s) > 1]
        for grp, cap in (
            ("SELECT at.album_id g,t.identity_key k FROM tracks t JOIN album_tracks at ON at.entry_id=t.id", max_album),
            ("SELECT ta.artist_id g, t.identity_key k FROM tracks t JOIN track_artists ta ON ta.entry_id=t.id", _ARTIST_BASKET_CAP)):
            buckets = {}
            for r in self.conn.execute(grp):
                if r["k"] in good:
                    buckets.setdefault(r["g"], set()).add(r["k"])
            out += [list(s) for s in buckets.values() if 1 < len(s) <= cap]
        sbuckets: dict = {}
        for r in self.conn.execute(
                "SELECT snapshot_id g,identity_key k FROM listening_daily_items"):
            if r["k"] in good:
                sbuckets.setdefault(r["g"], set()).add(r["k"])
        out += [list(s) for s in sbuckets.values() if 1 < len(s) <= max_session]
        fam, yr = {}, {}
        for r in self.conn.execute("SELECT genre, mb_year, identity_key k FROM tracks "
                                   "WHERE genre<>'' OR mb_year<>''"):
            if r["k"] not in good:
                continue
            if r["genre"]:
                fam.setdefault(genre_map.family(r["genre"]), set()).add(r["k"])
            dec = decade(r["mb_year"])
            if dec is not None:
                yr.setdefault(dec, set()).add(r["k"])
        out += [list(s) for s in fam.values() if 1 < len(s) <= _CONTENT_BASKET_CAP]
        out += [list(s) for s in yr.values() if 1 < len(s) <= _CONTENT_BASKET_CAP]
        return out

    @snapshot_read
    def rec_baskets_as_of(self, cutoff, max_album=30, max_session=120) -> tuple[list[list[str]], dict]:
        """Conservative baskets proven to exist by cutoff, plus coverage disclosure.

        Admit only unchanged, previously seen playlists. Their memberships can
        prove existence for legacy tracks without created_at. Mutable genre/year
        enrichment and undated album/artist relationships are excluded;
        listening-session evidence is strictly before cutoff.
        """
        eligibility = self._store.rec_eligibility
        excl = eligibility.excluded_playlist_ids() | eligibility.catchall_playlist_ids()
        safe_playlists = {r["id"] for r in self.conn.execute(
            "SELECT id FROM playlists WHERE first_seen IS NOT NULL AND first_seen<=? "
            "AND last_changed IS NOT NULL AND last_changed<=?", (cutoff, cutoff))}
        safe_playlists -= excl
        proven_by_playlist = set()
        if safe_playlists:
            qs = ",".join("?" * len(safe_playlists))
            proven_by_playlist = {r["k"] for r in self.conn.execute(
                f"SELECT DISTINCT t.identity_key k FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
                f"WHERE pt.playlist_id IN ({qs})", list(safe_playlists))}
        eligible = {r["k"] for r in self.conn.execute(
            "SELECT DISTINCT identity_key k FROM tracks "
            "WHERE (video_type IS NULL OR video_type <> 'MUSIC_VIDEO_TYPE_UGC') "
            f"AND {eligibility.track_length_clause()}")}
        dated = {r["k"] for r in self.conn.execute(
            "SELECT DISTINCT identity_key k FROM tracks WHERE created_at IS NOT NULL AND created_at<=?",
            (cutoff,))}
        good = (dated | proven_by_playlist) & eligible
        good -= eligibility.generated_only_keys()
        out = []
        pbuckets = {}
        if safe_playlists:
            qs = ",".join("?" * len(safe_playlists))
            for r in self.conn.execute(
                    f"SELECT pt.playlist_id g, t.identity_key k FROM playlist_tracks pt "
                    f"JOIN tracks t ON t.id=pt.track_id WHERE pt.playlist_id IN ({qs})",
                    list(safe_playlists)):
                if r["k"] in good:
                    pbuckets.setdefault(r["g"], set()).add(r["k"])
        out += [list(s) for s in pbuckets.values() if len(s) > 1]
        # These relationships have no observation timestamp. Current enrichment
        # cannot establish that the same co-membership existed before cutoff.
        sbuckets = {}
        for r in self.conn.execute(
                "SELECT DISTINCT identity_id||':'||day g,identity_key k FROM listening_activity WHERE at<?", (cutoff,)):
            if r["k"] in good:
                sbuckets.setdefault(r["g"], set()).add(r["k"])
        out += [list(s) for s in sbuckets.values() if 1 < len(s) <= max_session]
        total_playlists = self.conn.execute(
            "SELECT COUNT(*) n FROM playlists WHERE first_seen IS NOT NULL AND first_seen<=?", (cutoff,)).fetchone()["n"]
        coverage = {"eligible_tracks": len(good), "safe_playlists": len(safe_playlists),
                    "known_playlists": int(total_playlists), "session_baskets": len(sbuckets),
                    "mutable_content_baskets_excluded": True,
                    "undated_relationship_baskets_excluded": ["album", "artist"]}
        return out, coverage
