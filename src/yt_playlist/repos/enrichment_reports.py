"""SQLite coverage, conflict facts and references to retained provider evidence."""
import json

from yt_playlist.core import metadata, metadata_runtime, recording_library
from yt_playlist.core.album_queries import TRACK_LINKS
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized
from yt_playlist.repos.enrichment_sql import TRACKS_CTE, pending_coverage
from yt_playlist.repos import metadata_file_writes


class EnrichmentReportsRepo(Repo):
    @synchronized
    def genre_provenance_facts(self, track_id):
        track = self.conn.execute(
            "SELECT id, recording_id, video_id, title, artist, COALESCE(genre, '') genre FROM catalog_tracks WHERE id=?", (track_id,)
        ).fetchone()
        if track is None:
            return None
        rows = self.conn.execute(
            "WITH relevant AS (SELECT * FROM enrichment_log WHERE field='genre' AND "
            "((field_scope='recording' AND (recording_id=? OR input_video_id IN "
            "(SELECT video_id FROM track_sources WHERE recording_id=?))) OR "
            "(field_scope='legacy_item' AND (input_video_id=? OR track_id=?)))) "
            "SELECT provider,value FROM relevant e WHERE id=(SELECT MAX(id) FROM relevant newer "
            "WHERE newer.provider=e.provider) ORDER BY id",
            (track['recording_id'],track['recording_id'],track['video_id'],track_id)
        ).fetchall()
        candidates = [{'provider': row['provider'], 'value': row['value'] or ''} for row in rows]
        runtime = metadata_runtime.active(self.conn)
        references = [dict(row) for row in self.conn.execute(
            'SELECT s.recording_id,s.provider,p.metadata_provider_id provider_id,s.at FROM enrichment_provider_state s '
            'JOIN enrichment_provider_config p ON p.provider=s.provider AND p.metadata_provider_id IS NOT NULL '
            "WHERE s.recording_id=? AND s.status<>'queued'",
            (track['recording_id'],))] if runtime else []
        for reference in references:
            reference['file'] = metadata_file_writes.reference(self.conn, 'alternatives',
                (reference['recording_id'], reference['provider_id']), float(reference['at']))
        conflict = self.conn.execute(
            "SELECT candidates FROM enrichment_conflict WHERE track_id=? AND field='genre'", (track_id,)
        ).fetchone()
        return {'track': dict(track), 'candidates': candidates, 'runtime': runtime,
                'provider_references': references,
                'conflict': self._candidate_evidence(track_id, 'genre', conflict['candidates']) if conflict else None}

    def _candidate_evidence(self, track_id, field, encoded):
        if metadata_runtime.active(self.conn):
            row = metadata_runtime.conflict(self.conn, track_id, field)
            return {'reference': (row['id'], row['revision']), 'updated_at': row['updated_at'],
                'file': metadata_file_writes.reference(self.conn, 'conflicts', (row['id'],), row['revision'])} if row else None
        return {'candidates': json.loads(encoded)}

    @synchronized
    def conflict_count_for_playlist(self, playlist_id) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM enrichment_conflict c "
            "JOIN playlist_tracks pt ON pt.track_id=c.track_id "
            "WHERE pt.playlist_id=? AND c.resolved=0 AND c.field<>'energy'", (playlist_id,)).fetchone()
        return row["n"]

    @synchronized
    def conflict_count_for_album(self, album_browse_id) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM enrichment_conflict c "
            "JOIN catalog_tracks t ON t.id=c.track_id "
            f"WHERE EXISTS(SELECT 1 FROM {TRACK_LINKS} a WHERE a.entry_id=t.id AND a.browse_id=?) "
            "AND c.resolved=0 AND c.field<>'energy'", (album_browse_id,)).fetchone()
        return row["n"]

    def _conflicts(self, rows) -> list:
        return [{"track_id": r["track_id"], "title": r["title"], "artist": r["artist"],
                 "video_id": r["video_id"], "mb_recording_id": r["mb_recording_id"], "field": r["field"],
                 'evidence': self._candidate_evidence(r['track_id'], r['field'], r['candidates'])} for r in rows]

    @synchronized
    def unresolved_conflicts_for_playlist(self, playlist_id) -> list:
        rows = self.conn.execute(
            "SELECT c.track_id, c.field, c.candidates, t.title, t.artist, t.video_id, t.mb_recording_id "
            "FROM enrichment_conflict c "
            "JOIN playlist_tracks pt ON pt.track_id=c.track_id "
            "JOIN catalog_tracks t ON t.id=c.track_id "
            "WHERE pt.playlist_id=? AND c.resolved=0 AND c.field<>'energy' ORDER BY t.title, c.field",
            (playlist_id,)).fetchall()
        return self._conflicts(rows)

    @synchronized
    def unresolved_conflicts_for_album(self, album_browse_id) -> list:
        rows = self.conn.execute(
            "SELECT c.track_id, c.field, c.candidates, t.title, t.artist, t.video_id, t.mb_recording_id "
            "FROM enrichment_conflict c JOIN catalog_tracks t ON t.id=c.track_id "
            f"WHERE EXISTS(SELECT 1 FROM {TRACK_LINKS} a WHERE a.entry_id=t.id AND a.browse_id=?) "
            "AND c.resolved=0 AND c.field<>'energy' ORDER BY t.title, c.field",
            (album_browse_id,)).fetchall()
        return self._conflicts(rows)

    @snapshot_read
    def coverage_stats(self, include_discovery=False) -> dict:
        # Onboarding measures the library; the enrichment page also includes discovery.
        scope = "" if include_discovery else " WHERE t.in_library"
        r = self.conn.execute(
            "WITH " + TRACKS_CTE + " SELECT COUNT(*) total,"
            "SUM(first_enriched_at IS NOT NULL AND NOT " + pending_coverage() + ") processed,"
            "SUM(genre IS NOT NULL AND genre<>'') genre,SUM(mb_year IS NOT NULL AND mb_year<>'') year,"
            "SUM(bpm IS NOT NULL) bpm,SUM(danceability IS NOT NULL) danceability "
            "FROM enrichment_tracks t" + scope).fetchone()
        return {k: (r[k] or 0) for k in
                ("total", "processed", "genre", "year", "bpm", "danceability")}

    @snapshot_read
    def queue_remaining(self) -> int:
        return self.conn.execute(
            "WITH " + TRACKS_CTE +
            " SELECT COUNT(*) n FROM enrichment_tracks t WHERE first_enriched_at IS NULL OR " +
            pending_coverage()).fetchone()["n"]

    @lookup_read
    def has_pending_tracks(self) -> bool:
        # Pending status needs a usable input, but does not choose which one.
        return self.conn.execute(f'''
            WITH scope AS ({recording_library.ENRICHMENT_SQL})
            SELECT 1 FROM scope JOIN recordings r ON r.id=scope.recording_id
            JOIN track_sources input ON input.recording_id=r.id AND (scope.in_library OR input.id=r.discovery_entry_id)
            LEFT JOIN enrichment_provider_state s ON s.recording_id=r.id AND s.queued=1
            LEFT JOIN enrichment_provider_config c ON c.provider=s.provider AND c.status='available'
            WHERE r.first_enriched_at IS NULL OR c.provider IS NOT NULL
            LIMIT 1
        ''').fetchone() is not None

    @snapshot_read
    def queue_stats(self) -> dict:
        """Separate first passes from refreshes without counting provider jobs as tracks."""
        row = self.conn.execute(
            "WITH " + TRACKS_CTE +
            " SELECT COUNT(*) total, SUM(first_enriched_at IS NULL) first_pass, "
            "SUM(first_enriched_at IS NOT NULL) refresh FROM enrichment_tracks t "
            "WHERE first_enriched_at IS NULL OR " + pending_coverage()).fetchone()
        return {key: row[key] or 0 for key in ("total", "first_pass", "refresh")}

    @synchronized
    def outstanding_conflicts(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM enrichment_conflict WHERE resolved=0 AND field<>'energy'").fetchone()["n"]

    @synchronized
    def activity_stats(self, since) -> dict:
        row = self.conn.execute(
            "WITH " + TRACKS_CTE +
            " SELECT MAX(last_enriched_at) latest, SUM(last_enriched_at >= ?) recent "
            "FROM enrichment_tracks", (since,)).fetchone()
        return {"latest": row["latest"], "recent": row["recent"] or 0}

    @synchronized
    def enriched_tracks(self, limit, offset) -> list:
        """Completed passes, newest first; missing metadata does not imply a failed pass."""
        rows = self.conn.execute(
            "WITH " + TRACKS_CTE +
            " SELECT id, title, artist, genre, mb_year, bpm, danceability, last_enriched_at "
            "FROM enrichment_tracks t WHERE first_enriched_at IS NOT NULL AND NOT " + pending_coverage() + " "
            "ORDER BY last_enriched_at DESC, queue_order DESC, source ASC LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def unresolved_enrichment_conflicts(self, limit, offset, *, fields):
        rows = self.conn.execute(
            self._review_select(fields) + " WHERE c.resolved=0 AND c.field<>'energy' "
            "ORDER BY t.title COLLATE NOCASE, t.id, c.field LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()
        return self._review_facts(rows, fields)

    def _review_select(self, fields):
        columns = ", ".join("t." + col for col in sorted(set(fields.values())))
        return ("SELECT c.track_id, c.field, c.candidates, c.resolved_value, "
                "t.title, t.artist, t.video_id, t.mb_recording_id, "
                f"{columns} FROM enrichment_conflict c JOIN catalog_tracks t ON t.id=c.track_id")

    def _review_facts(self, rows, fields):
        result = self._conflicts(rows)
        for conflict, row in zip(result, rows):
            column = fields.get(conflict['field'])
            current = row[column] if column else None
            conflict["current"] = current
            conflict['selected_source'] = (metadata.selected_source(self.conn, conflict['track_id'], column or conflict['field'])
                                           if metadata_runtime.active(self.conn) and current not in (None, '') else None)
            conflict['findings'] = [dict(finding) for finding in self.conn.execute(
                "SELECT provider,value FROM enrichment_log "
                'WHERE track_id=? AND field=? ORDER BY id', (conflict['track_id'], conflict['field']))]
            conflict['overridden'] = metadata.has_choice(self.conn, conflict['track_id'], column or conflict['field'])
        return result

    @synchronized
    def enrichment_conflict_detail(self, track_id, field, *, fields):
        if field not in fields:
            return None
        rows = self.conn.execute(self._review_select(fields) + " WHERE c.track_id=? AND c.field=?",
                                 (track_id, field)).fetchall()
        return self._review_facts(rows, fields)[0] if rows else None

    @synchronized
    def processed_timestamps(self):
        rows = self.conn.execute(
            "WITH " + TRACKS_CTE +
            " SELECT first_enriched_at first, last_enriched_at last "
            "FROM enrichment_tracks WHERE first_enriched_at IS NOT NULL "
            "ORDER BY first_enriched_at").fetchall()
        return [dict(row) for row in rows]
