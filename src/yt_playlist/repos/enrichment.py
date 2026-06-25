"""EnrichmentRepo — the parseable per-provider response log and the derived disagreement records.

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
