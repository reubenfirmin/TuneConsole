"""EnrichmentRepo: the parseable per-provider response log and the derived disagreement records.

The waterfall harness writes one `enrichment_log` row per (run, track, provider, field) and, when
providers disagree on a field, upserts an `enrichment_conflict` (one per track+field). The playlist /
album UI reads the conflict counts (for the header icon) and the unresolved list (for the resolver);
resolving overwrites the canonical track column and marks the conflict resolved.
"""
import json
import time

from yt_playlist.repos.base import Repo, synchronized

# field concept name -> (tracks column, caster). The whitelist that makes a user-chosen resolution
# safe to write straight into the canonical column.
_FIELD_COLUMN = {
    "genre": ("genre", str), "year": ("mb_year", str), "label": ("label", str),
    "music_key": ("music_key", str), "music_scale": ("music_scale", str),
    "bpm": ("bpm", float), "energy": ("energy", float), "danceability": ("danceability", float),
    "popularity": ("popularity", int), "gain": ("gain", float),
}


class EnrichmentRepo(Repo):
    @synchronized
    def log_enrichment(self, track_id, run_id, provider, field, value, now=None) -> None:
        self.conn.execute(
            "INSERT INTO enrichment_log(track_id, run_id, provider, field, value, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (track_id, run_id, provider, field,
             None if value is None else str(value), now or time.time()))
        self.conn.commit()

    @synchronized
    def upsert_conflict(self, track_id, field, candidates, now=None) -> None:
        """Record (or refresh) a disagreement for one track+field. `candidates` is a list of
        {"provider","value"}. Re-run rule: refresh the candidate list; a previously-resolved conflict
        stays resolved while its candidate *values* are unchanged, but reopens if a new value appears."""
        payload = json.dumps(candidates)
        new_values = {str(c["value"]) for c in candidates}
        row = self.conn.execute(
            "SELECT candidates, resolved FROM enrichment_conflict WHERE track_id=? AND field=?",
            (track_id, field)).fetchone()
        reopen = False
        if row is not None and row["resolved"]:
            old_values = {str(c["value"]) for c in json.loads(row["candidates"])}
            reopen = bool(new_values - old_values)        # a genuinely new option showed up
        resolved_clause = "0" if (row is None or reopen) else "resolved"
        self.conn.execute(
            "INSERT INTO enrichment_conflict(track_id, field, candidates, resolved, updated_at) "
            "VALUES (?,?,?,0,?) "
            "ON CONFLICT(track_id, field) DO UPDATE SET "
            f"  candidates=excluded.candidates, resolved={resolved_clause}, updated_at=excluded.updated_at",
            (track_id, field, payload, now or time.time()))
        self.conn.commit()

    @synchronized
    def set_track_field(self, track_id, field, value) -> None:
        """Overwrite a single canonical track column (used by conflict resolution). Whitelisted."""
        spec = _FIELD_COLUMN.get(field)
        if spec is None:
            raise ValueError(f"not a writable enrichment field: {field!r}")
        col, cast = spec
        try:
            v = cast(value) if value not in (None, "") else ("" if cast is str else None)
        except (TypeError, ValueError):
            v = value
        self.conn.execute(f"UPDATE tracks SET {col}=? WHERE id=?", (v, track_id))
        self.conn.commit()

    @synchronized
    def resolve_conflict(self, track_id, field, value, now=None) -> None:
        """Apply the user's choice: overwrite the canonical column and mark the conflict resolved."""
        self.set_track_field(track_id, field, value)
        self.conn.execute(
            "UPDATE enrichment_conflict SET resolved=1, resolved_value=?, updated_at=? "
            "WHERE track_id=? AND field=?",
            (None if value is None else str(value), now or time.time(), track_id, field))
        self.conn.commit()

    @synchronized
    def conflict_count_for_playlist(self, playlist_id) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM enrichment_conflict c "
            "JOIN playlist_tracks pt ON pt.track_id=c.track_id "
            "WHERE pt.playlist_id=? AND c.resolved=0", (playlist_id,)).fetchone()
        return row["n"]

    @synchronized
    def conflict_count_for_album(self, album_browse_id) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM enrichment_conflict c "
            "JOIN tracks t ON t.id=c.track_id "
            "WHERE t.album_browse_id=? AND c.resolved=0", (album_browse_id,)).fetchone()
        return row["n"]

    def _conflicts(self, rows) -> list:
        return [{"track_id": r["track_id"], "title": r["title"], "artist": r["artist"],
                 "video_id": r["video_id"], "field": r["field"],
                 "candidates": json.loads(r["candidates"])} for r in rows]

    @synchronized
    def unresolved_conflicts_for_playlist(self, playlist_id) -> list:
        rows = self.conn.execute(
            "SELECT c.track_id, c.field, c.candidates, t.title, t.artist, t.video_id "
            "FROM enrichment_conflict c "
            "JOIN playlist_tracks pt ON pt.track_id=c.track_id "
            "JOIN tracks t ON t.id=c.track_id "
            "WHERE pt.playlist_id=? AND c.resolved=0 ORDER BY t.title, c.field",
            (playlist_id,)).fetchall()
        return self._conflicts(rows)

    @synchronized
    def unresolved_conflicts_for_album(self, album_browse_id) -> list:
        rows = self.conn.execute(
            "SELECT c.track_id, c.field, c.candidates, t.title, t.artist, t.video_id "
            "FROM enrichment_conflict c JOIN tracks t ON t.id=c.track_id "
            "WHERE t.album_browse_id=? AND c.resolved=0 ORDER BY t.title, c.field",
            (album_browse_id,)).fetchall()
        return self._conflicts(rows)

    # --- enrichment worker: priority queue, processed bookkeeping, coverage stats ----------------

    # Per-track song plays + the play sums of the containers it belongs to (playlist / album / artist).
    # Reused by the priority queue. `tp` = track plays; the three container CTEs aggregate over members.
    _QUEUE_CTES = """
        WITH plays AS (SELECT identity_key, COUNT(*) c FROM history_items GROUP BY identity_key),
        tp AS (SELECT t.id, COALESCE(p.c,0) sp FROM tracks t
               LEFT JOIN plays p ON p.identity_key=t.identity_key),
        pl_plays AS (SELECT pt.playlist_id, SUM(tp.sp) c FROM playlist_tracks pt
                     JOIN tp ON tp.id=pt.track_id GROUP BY pt.playlist_id),
        al_plays AS (SELECT t.album_browse_id ab, SUM(tp.sp) c FROM tracks t JOIN tp ON tp.id=t.id
                     WHERE t.album_browse_id IS NOT NULL GROUP BY t.album_browse_id),
        ar_plays AS (SELECT t.artist ar, SUM(tp.sp) c FROM tracks t JOIN tp ON tp.id=t.id
                     WHERE t.artist IS NOT NULL AND t.artist<>'' GROUP BY t.artist)
    """

    @synchronized
    def next_enrich_batch(self, limit) -> list:
        """The worker's priority queue: up to `limit` not-yet-processed tracks, ordered by tier:
        (0) new arrivals (created after the worker last caught up), (1) played songs by playcount,
        (2) zero-play songs in a played playlist/album/artist by that container's plays, (3) orphans."""
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key='enrich_caught_up_at'").fetchone()
        caught_up = float(row["value"]) if row and row["value"] else 0.0
        rows = self.conn.execute(self._QUEUE_CTES + """
            SELECT t.id, t.video_id, t.title, t.artist, t.mb_recording_id, tp.sp,
              MAX(COALESCE((SELECT MAX(plp.c) FROM playlist_tracks pt JOIN pl_plays plp
                            ON plp.playlist_id=pt.playlist_id WHERE pt.track_id=t.id), 0),
                  COALESCE((SELECT c FROM al_plays WHERE ab=t.album_browse_id), 0),
                  COALESCE((SELECT c FROM ar_plays WHERE ar=t.artist), 0)) AS best
            FROM tracks t JOIN tp ON tp.id=t.id
            WHERE t.first_enriched_at IS NULL
            ORDER BY
              CASE WHEN t.created_at IS NOT NULL AND t.created_at > ? THEN 0
                   WHEN tp.sp > 0 THEN 1
                   WHEN best > 0 THEN 2 ELSE 3 END ASC,
              CASE WHEN t.created_at IS NOT NULL AND t.created_at > ? THEN -t.created_at
                   WHEN tp.sp > 0 THEN -tp.sp ELSE -best END ASC,
              t.id ASC
            LIMIT ?""", (caught_up, caught_up, limit)).fetchall()
        return [{"id": r["id"], "video_id": r["video_id"], "title": r["title"],
                 "artist": r["artist"], "mb_recording_id": r["mb_recording_id"]} for r in rows]

    @synchronized
    def resweep_batch(self, limit, stale_before) -> list:
        """Already-processed tracks that are stale (last_enriched_at < stale_before) AND still missing
        a core field, re-attempted only when the primary queue is empty. Oldest-checked first."""
        rows = self.conn.execute(
            "SELECT id, video_id, title, artist, mb_recording_id FROM tracks "
            "WHERE first_enriched_at IS NOT NULL AND last_enriched_at < ? "
            "AND (genre IS NULL OR genre='' OR mb_year IS NULL OR mb_year='' "
            "     OR bpm IS NULL OR energy IS NULL OR danceability IS NULL) "
            "ORDER BY last_enriched_at ASC LIMIT ?", (stale_before, limit)).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def mark_enriched(self, track_ids, now) -> None:
        """Stamp a processed pass: set first_enriched_at once, bump last_enriched_at every time."""
        if not track_ids:
            return
        qs = ",".join("?" * len(track_ids))
        self.conn.execute(
            f"UPDATE tracks SET first_enriched_at = COALESCE(first_enriched_at, ?), "
            f"last_enriched_at = ? WHERE id IN ({qs})", [now, now, *track_ids])
        self.conn.commit()

    @synchronized
    def coverage_stats(self) -> dict:
        r = self.conn.execute(
            "SELECT COUNT(*) total, "
            "  SUM(first_enriched_at IS NOT NULL) processed, "
            "  SUM(genre IS NOT NULL AND genre<>'') genre, "
            "  SUM(mb_year IS NOT NULL AND mb_year<>'') year, "
            "  SUM(bpm IS NOT NULL) bpm, SUM(energy IS NOT NULL) energy, "
            "  SUM(danceability IS NOT NULL) danceability FROM tracks").fetchone()
        return {k: (r[k] or 0) for k in
                ("total", "processed", "genre", "year", "bpm", "energy", "danceability")}

    @synchronized
    def queue_remaining(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM tracks WHERE first_enriched_at IS NULL").fetchone()["n"]

    @synchronized
    def outstanding_conflicts(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM enrichment_conflict WHERE resolved=0").fetchone()["n"]

    @synchronized
    def processed_timeline(self, buckets=40) -> list:
        """Cumulative processed-track count over time, as up to `buckets` (t, cumulative) points,
        for the trend sparkline. Empty list when nothing's been processed yet."""
        rng = self.conn.execute(
            "SELECT MIN(first_enriched_at) lo, MAX(first_enriched_at) hi, COUNT(*) n "
            "FROM tracks WHERE first_enriched_at IS NOT NULL").fetchone()
        if not rng["n"] or rng["lo"] is None:
            return []
        lo, hi, total = rng["lo"], rng["hi"], rng["n"]
        if hi <= lo:
            return [{"t": hi, "n": total}]
        width = (hi - lo) / buckets
        out, cum, i = [], 0, 0
        rows = self.conn.execute(
            "SELECT first_enriched_at e FROM tracks WHERE first_enriched_at IS NOT NULL "
            "ORDER BY first_enriched_at").fetchall()
        for b in range(1, buckets + 1):
            edge = lo + width * b
            while i < len(rows) and rows[i]["e"] <= edge:
                cum += 1
                i += 1
            out.append({"t": edge, "n": cum})
        out[-1]["n"] = total
        return out
