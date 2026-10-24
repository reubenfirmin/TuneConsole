"""Read-only coverage, provenance and conflict review projections.

Writable field names and conflict choices belong to enrichment.py. Provider precedence
belongs to core.metadata; these projections do not duplicate either mutation policy.
"""
import json

from yt_playlist.core import catalog, metadata
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos.enrichment import FIELD_COLUMN
from yt_playlist.repos.enrichment_sql import TRACKS_CTE, pending_coverage


class EnrichmentReportsRepo(Repo):
    @synchronized
    def metadata_provenance(self, track_id):
        track_id = catalog.resolve_entry(self.conn,track_id)
        track = self.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (track_id,)).fetchone()
        if track is None:
            return None
        choices = {r['field']:dict(r) for r in self.conn.execute(
            'SELECT * FROM entry_field_choices WHERE entry_id=?', (track_id,))}
        fields = {}
        for field in (*catalog.ITEM_FIELDS,*catalog.RECORDING_FIELDS):
            choice = choices.get(field)
            fields[field] = {'value':track[field], 'override':choice is not None,
                             'source':choice['source'] if choice else
                             metadata.selected_source(self.conn,track_id,field) or
                             ('missing' if track[field] is None else 'unknown')}
        decisions = [dict(r) for r in self.conn.execute(
            'SELECT * FROM metadata_decisions WHERE entry_id=? ORDER BY chosen_at,id', (track_id,))]
        return {'fields':fields,'decisions':decisions}

    @synchronized
    def genre_provenance(self, track_id) -> dict | None:
        """Current genre plus the latest retained answer from each enrichment provider."""
        track_id = catalog.resolve_entry(self.conn, track_id)
        track = self.conn.execute(
            "SELECT id, recording_id, video_id, title, artist, COALESCE(genre, '') genre FROM catalog_tracks WHERE id=?", (track_id,)
        ).fetchone()
        if track is None:
            return None
        rows = self.conn.execute(
            "WITH relevant AS (SELECT * FROM enrichment_log WHERE field='genre' AND "
            "((field_scope='recording' AND (recording_id=? OR input_video_id IN "
            "(SELECT video_id FROM youtube_items WHERE recording_id=?))) OR "
            "(field_scope='legacy_item' AND (input_video_id=? OR track_id=?)))) "
            "SELECT provider,value FROM relevant e WHERE id=(SELECT MAX(id) FROM relevant newer "
            "WHERE newer.provider=e.provider) ORDER BY id",
            (track['recording_id'],track['recording_id'],track['video_id'],track_id)
        ).fetchall()
        candidates = [{"provider": r["provider"], "value": r["value"] or ""} for r in rows]
        conflict = self.conn.execute(
            "SELECT candidates FROM enrichment_conflict WHERE track_id=? AND field='genre'", (track_id,)
        ).fetchone()
        seen = {(c["provider"], c["value"]) for c in candidates}
        if conflict:
            for c in json.loads(conflict["candidates"]):
                pair = (str(c.get("provider") or "unknown"), str(c.get("value") or ""))
                if pair not in seen:
                    candidates.append({"provider": pair[0], "value": pair[1]})
                    seen.add(pair)
        options = []
        by_value = {}
        for candidate in candidates:
            value = candidate["value"]
            if not value:
                continue
            option = by_value.get(value)
            if option is None:
                option = {"value": value, "providers": []}
                by_value[value] = option
                options.append(option)
            if candidate["provider"] not in option["providers"]:
                option["providers"].append(candidate["provider"])
        return {"track_id": track["id"], "video_id": track["video_id"],
                "title": track["title"], "artist": track["artist"],
                "current": track["genre"], "candidates": candidates, "options": options}

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
            "JOIN catalog_tracks t ON t.id=c.track_id "
            "WHERE EXISTS(SELECT 1 FROM album_track_links a WHERE a.entry_id=t.id AND a.browse_id=?) "
            "AND c.resolved=0", (album_browse_id,)).fetchone()
        return row["n"]

    def _conflicts(self, rows) -> list:
        return [{"track_id": r["track_id"], "title": r["title"], "artist": r["artist"],
                 "video_id": r["video_id"], "mb_recording_id": r["mb_recording_id"], "field": r["field"],
                 "candidates": json.loads(r["candidates"])} for r in rows]

    @synchronized
    def unresolved_conflicts_for_playlist(self, playlist_id) -> list:
        rows = self.conn.execute(
            "SELECT c.track_id, c.field, c.candidates, t.title, t.artist, t.video_id, t.mb_recording_id "
            "FROM enrichment_conflict c "
            "JOIN playlist_tracks pt ON pt.track_id=c.track_id "
            "JOIN catalog_tracks t ON t.id=c.track_id "
            "WHERE pt.playlist_id=? AND c.resolved=0 ORDER BY t.title, c.field",
            (playlist_id,)).fetchall()
        return self._conflicts(rows)

    @synchronized
    def unresolved_conflicts_for_album(self, album_browse_id) -> list:
        rows = self.conn.execute(
            "SELECT c.track_id, c.field, c.candidates, t.title, t.artist, t.video_id, t.mb_recording_id "
            "FROM enrichment_conflict c JOIN catalog_tracks t ON t.id=c.track_id "
            "WHERE EXISTS(SELECT 1 FROM album_track_links a WHERE a.entry_id=t.id AND a.browse_id=?) "
            "AND c.resolved=0 ORDER BY t.title, c.field",
            (album_browse_id,)).fetchall()
        return self._conflicts(rows)

    @synchronized
    def coverage_stats(self, include_discovery=False) -> dict:
        # Onboarding measures the library; the enrichment page measures every queued source.
        prefix = "WITH " + TRACKS_CTE if include_discovery else ""
        table = "enrichment_tracks" if include_discovery else "tracks"
        r = self.conn.execute(
            prefix + " SELECT COUNT(*) total,SUM(processed) processed,SUM(genre) genre,"
            "SUM(year) year,SUM(bpm) bpm,SUM(energy) energy,SUM(danceability) danceability FROM ("
            "SELECT recording_id,MAX(first_enriched_at IS NOT NULL AND NOT " +
            pending_coverage() + ") processed,"
            "MAX(genre IS NOT NULL AND genre<>'') genre,MAX(mb_year IS NOT NULL AND mb_year<>'') year,"
            "MAX(bpm IS NOT NULL) bpm,MAX(energy IS NOT NULL) energy,"
            f"MAX(danceability IS NOT NULL) danceability FROM {table} t GROUP BY recording_id)").fetchone()
        return {k: (r[k] or 0) for k in
                ("total", "processed", "genre", "year", "bpm", "energy", "danceability")}

    @synchronized
    def queue_remaining(self) -> int:
        return self.conn.execute(
            "WITH " + TRACKS_CTE +
            " SELECT COUNT(*) n FROM enrichment_tracks t WHERE first_enriched_at IS NULL OR " +
            pending_coverage()).fetchone()["n"]

    @synchronized
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
            "SELECT COUNT(*) n FROM enrichment_conflict WHERE resolved=0").fetchone()["n"]

    @synchronized
    def enrichment_activity_stats(self, now) -> dict:
        row = self.conn.execute(
            "WITH " + TRACKS_CTE +
            " SELECT MAX(last_enriched_at) latest, SUM(last_enriched_at >= ?) recent "
            "FROM enrichment_tracks", (now - 900,)).fetchone()
        return {"latest": row["latest"], "recent": row["recent"] or 0}

    @synchronized
    def enriched_tracks(self, limit=40, offset=0) -> list:
        """Completed passes, newest first; missing metadata does not imply a failed pass."""
        rows = self.conn.execute(
            "WITH " + TRACKS_CTE +
            " SELECT id, title, artist, genre, mb_year, bpm, energy, danceability, last_enriched_at "
            "FROM enrichment_tracks t WHERE first_enriched_at IS NOT NULL AND NOT " + pending_coverage() + " "
            "ORDER BY last_enriched_at DESC, queue_order DESC, source ASC LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def unresolved_enrichment_conflicts(self, limit=20, offset=0) -> list:
        """Library-wide review, paged by field disagreement rather than song."""
        rows = self.conn.execute(
            self._review_select() + " WHERE c.resolved=0 "
            "ORDER BY t.title COLLATE NOCASE, t.id, c.field LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()
        return self._review_conflicts(rows)

    @staticmethod
    def _review_select():
        columns = ", ".join("t." + col for col in sorted({spec[0] for spec in FIELD_COLUMN.values()}))
        return ("SELECT c.track_id, c.field, c.candidates, c.resolved_value, "
                "t.title, t.artist, t.video_id, t.mb_recording_id, "
                f"{columns} FROM enrichment_conflict c JOIN catalog_tracks t ON t.id=c.track_id")

    def _review_conflicts(self, rows):
        """Offer saved values even when absent from provider candidates; label unknown sources."""
        result = self._conflicts(rows)
        for conflict, row in zip(result, rows):
            spec = FIELD_COLUMN.get(conflict["field"])
            current = row[spec[0]] if spec else None
            conflict["current"] = current
            conflict["can_resolve"] = spec is not None
            cast = spec[1] if spec else str

            def key(value):
                if value is None or value == "":
                    return None
                try:
                    value = cast(value)
                except (TypeError, ValueError):
                    pass
                return value.strip().casefold() if isinstance(value, str) else value

            current_key = key(current)
            options = [{"value": str(current) if current is not None else "",
                        "label": current if current is not None and current != "" else "Not set",
                        "current": True, "providers": [], "source_note": ""}]
            by_value = {current_key: options[0]}
            for candidate in conflict["candidates"]:
                value = candidate["value"]
                option = by_value.get(key(value))
                if option is None:
                    option = {"value": str(value) if value is not None else "",
                              "label": value if value not in (None,'') else "Not set", "current": False,
                              "providers": [], "source_note": ""}
                    by_value[key(value)] = option
                    options.append(option)
                if candidate["provider"] not in option["providers"]:
                    option["providers"].append(candidate["provider"])
            if not options[0]["providers"] and current_key is not None:
                findings = self.conn.execute(
                    "SELECT provider, value FROM enrichment_log WHERE track_id=? AND field=? ORDER BY id",
                    (conflict["track_id"], conflict["field"])).fetchall()
                for finding in findings:
                    if key(finding["value"]) == current_key and finding["provider"] not in options[0]["providers"]:
                        options[0]["providers"].append(finding["provider"])
            override = self.conn.execute('SELECT 1 FROM entry_field_choices WHERE entry_id=? AND field=?',
                                         (conflict['track_id'],spec[0] if spec else conflict['field'])).fetchone()
            if override:
                options[0]["source_note"] = "Your choice"
            elif not options[0]["providers"]:
                options[0]["source_note"] = ("No saved value" if current_key is None else
                                              "Saved locally; original source not recorded")
            for option in options:
                option["id"] = (f"enrich-choice-{conflict['track_id']}-{conflict['field']}-"
                                + option["value"].encode().hex())
            # Preserve alternative positions after saving, including keyboard focus.
            provider_order = {}
            for candidate in conflict["candidates"]:
                provider_order.setdefault(key(candidate["value"]), len(provider_order))
            options.sort(key=lambda option: provider_order.get(key(option["value"]), -1))
            conflict["options"] = options
        return result

    @synchronized
    def enrichment_conflict_detail(self, track_id, field) -> dict | None:
        track_id = catalog.resolve_entry(self.conn, track_id)
        rows = self.conn.execute(self._review_select() + " WHERE c.track_id=? AND c.field=?",
                                 (track_id, field)).fetchall()
        return self._review_conflicts(rows)[0] if rows else None

    @synchronized
    def processed_timeline(self, buckets=40) -> list:
        """Cumulative first checks and each track's latest re-enrichment on one timeline."""
        rows = self.conn.execute(
            "WITH " + TRACKS_CTE +
            " SELECT first_enriched_at first, last_enriched_at last "
            "FROM enrichment_tracks WHERE first_enriched_at IS NOT NULL "
            "ORDER BY first_enriched_at").fetchall()
        if not rows:
            return []
        refreshed = sorted(row["last"] for row in rows
                           if row["last"] is not None and row["last"] > row["first"])
        lo, hi, total = rows[0]["first"], max(rows[-1]["first"], max(refreshed, default=0)), len(rows)
        if hi <= lo:
            return [{"t": hi, "n": total, "refresh": 0}]
        width = (hi - lo) / buckets
        out, cum, i, refreshed_count = [], 0, 0, 0
        for b in range(1, buckets + 1):
            edge = lo + width * b
            while i < len(rows) and rows[i]["first"] <= edge:
                cum += 1
                i += 1
            while refreshed_count < len(refreshed) and refreshed[refreshed_count] <= edge:
                refreshed_count += 1
            out.append({"t": edge, "n": cum, "refresh": refreshed_count})
        out[-1]["n"] = total
        out[-1]["refresh"] = len(refreshed)
        return out
