"""Shared library eligibility, generated-playlist quarantine and feedback exclusions.

All reads use this repository's connection, including when called on a pinned reader.
Candidate ranking and historical basket construction consume these rules explicitly.
"""
from yt_playlist.core.recommendation_settings import MAX_TRACK_MINUTES
from yt_playlist.repos.base import GENERATED_GROUP, Repo, synchronized
from yt_playlist.repos.playlist_eligibility import QUARANTINED_PLAYLIST_IDS_SQL


def exclude_ids(column, ids, *, connective="AND") -> str:
    """SQL exclusion for internal integer playlist IDs, never untrusted input."""
    if not ids:
        return ""
    return f" {connective} {column} NOT IN ({','.join(str(int(i)) for i in ids)})"


class RecEligibilityRepo(Repo):
    @synchronized
    def track_length_clause(self, col="duration_s") -> str:
        """Keep tracks within the saved length limit, including unknown durations."""
        minutes = MAX_TRACK_MINUTES.parse(
            self._store.settings.get_setting('rec_param:' + MAX_TRACK_MINUTES.name))
        return f"({col} IS NULL OR {col} <= {60 * minutes})"

    @synchronized
    def suppressed_candidate_keys(self, keys, now) -> set:
        """Track/artist bans for loaded candidates; genre bans use their metadata."""
        keys = list(dict.fromkeys(keys))
        if not keys:
            return set()
        qs = ",".join("?" * len(keys))
        blocked = {r[0] for r in self.conn.execute(
            f"SELECT item_key FROM rec_feedback WHERE item_key IN ({qs}) "
            "AND ((surface='for_you' AND scope='' AND kind IN ('dismiss','mute','not_now')) "
            "OR kind='dislike') AND (until IS NULL OR until>?)", [*keys, now])}
        blocked.update(r[0] for r in self.conn.execute(
            "SELECT DISTINCT r.identity_key FROM rec_feedback f "
            "JOIN track_artists ta ON ta.artist_id=f.artist_id "
            "JOIN catalog_entries e ON e.id=ta.entry_id JOIN recordings r ON r.id=e.recording_id "
            f"WHERE r.identity_key IN ({qs}) AND f.kind='mute' AND (f.until IS NULL OR f.until>?)",
            [*keys, now]))
        blocked.update(r[0] for r in self.conn.execute(
            'SELECT DISTINCT t.identity_key FROM playlist_tracks pt '
            'JOIN tracks t ON t.id=pt.track_id JOIN playlists p ON p.id=pt.playlist_id '
            'JOIN quarantined_playlists g ON g.ytm=p.scope_ref '
            f'WHERE g.name=? AND t.identity_key IN ({qs})', [GENERATED_GROUP, *keys]))
        return blocked

    @synchronized
    def excluded_playlist_ids(self, group=GENERATED_GROUP) -> set:
        """Generated playlists stay excluded until adoption, regardless of group."""
        return {r['id'] for r in self.conn.execute(QUARANTINED_PLAYLIST_IDS_SQL, (group,))}

    @synchronized
    def generated_track_keys(self, group=GENERATED_GROUP) -> set:
        """Keys held in unadopted generated playlists, already spoken for."""
        return {r["identity_key"] for r in self.conn.execute(
            "SELECT DISTINCT t.identity_key FROM playlist_tracks pt "
            "JOIN tracks t ON t.id=pt.track_id JOIN playlists p ON p.id=pt.playlist_id "
            "JOIN quarantined_playlists g ON g.ytm=p.scope_ref WHERE g.name=?", (group,))}

    @synchronized
    def generated_only_keys(self, group=GENERATED_GROUP) -> set:
        """Keys present only in quarantined playlists; plays never lift quarantine."""
        excl = self.excluded_playlist_ids(group)
        if not excl:
            return set()
        qs = ",".join("?" * len(excl))
        rows = self.conn.execute(
            "SELECT t.identity_key k FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id "
            "GROUP BY t.identity_key "
            f"HAVING SUM(CASE WHEN pt.playlist_id IN ({qs}) THEN 0 ELSE 1 END)=0",
            list(excl)).fetchall()
        return {r["k"] for r in rows}

    @synchronized
    def catchall_playlist_ids(self, size_floor=120) -> set:
        """Overlarge playlists carry weak, low-information co-occurrence signal."""
        return {r["pid"] for r in self.conn.execute(
            "SELECT playlist_id pid, COUNT(*) n FROM playlist_tracks GROUP BY playlist_id HAVING n > ?",
            (size_floor,))}
