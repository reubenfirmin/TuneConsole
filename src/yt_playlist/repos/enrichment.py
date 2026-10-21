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
    def record_enrichment_attempt(self, identity_key, provider, fields, *, retry=False, now=None):
        """Retain the latest provider answers by identity while canonical track storage is deferred.

        The existing settings KV holds this temporary audit data; it is not a second work queue.
        Empty successful answers are retained too, so a miss can be distinguished from no lookup.
        """
        key = "enrich_attempts:" + identity_key
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        attempts = json.loads(row["value"]) if row else {}
        attempts[provider] = {"at": time.time() if now is None else now,
                              "status": "retry" if retry else ("found" if fields else "no_match"),
                              "fields": fields}
        self.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                          (key, json.dumps(attempts)))
        self.conn.commit()

    @synchronized
    def genre_provenance(self, track_id) -> dict | None:
        """Current genre plus the latest retained answer from each enrichment provider."""
        track = self.conn.execute(
            "SELECT id, title, artist, COALESCE(genre, '') genre FROM tracks WHERE id=?", (track_id,)
        ).fetchone()
        if track is None:
            return None
        rows = self.conn.execute(
            "SELECT provider, value FROM enrichment_log e WHERE track_id=? AND field='genre' "
            "AND id=(SELECT MAX(id) FROM enrichment_log newer WHERE newer.track_id=e.track_id "
            "AND newer.field=e.field AND newer.provider=e.provider) ORDER BY id", (track_id,)
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
        return {"track_id": track["id"], "title": track["title"], "artist": track["artist"],
                "current": track["genre"], "candidates": candidates, "options": options}

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
                 "video_id": r["video_id"], "mb_recording_id": r["mb_recording_id"], "field": r["field"],
                 "candidates": json.loads(r["candidates"])} for r in rows]

    @synchronized
    def unresolved_conflicts_for_playlist(self, playlist_id) -> list:
        rows = self.conn.execute(
            "SELECT c.track_id, c.field, c.candidates, t.title, t.artist, t.video_id, t.mb_recording_id "
            "FROM enrichment_conflict c "
            "JOIN playlist_tracks pt ON pt.track_id=c.track_id "
            "JOIN tracks t ON t.id=c.track_id "
            "WHERE pt.playlist_id=? AND c.resolved=0 ORDER BY t.title, c.field",
            (playlist_id,)).fetchall()
        return self._conflicts(rows)

    @synchronized
    def unresolved_conflicts_for_album(self, album_browse_id) -> list:
        rows = self.conn.execute(
            "SELECT c.track_id, c.field, c.candidates, t.title, t.artist, t.video_id, t.mb_recording_id "
            "FROM enrichment_conflict c JOIN tracks t ON t.id=c.track_id "
            "WHERE t.album_browse_id=? AND c.resolved=0 ORDER BY t.title, c.field",
            (album_browse_id,)).fetchall()
        return self._conflicts(rows)

    # --- enrichment worker: priority queue, processed bookkeeping, coverage stats ----------------

    # One logical track stream over the existing storage. Discovery membership and canonical track
    # storage will be consolidated separately; queue ordering must not depend on the source table.
    _TRACKS_CTE = """
        enrichment_tracks AS (
            SELECT 'library' source, id, id queue_order, identity_key, video_id, title, artist,
                   album_browse_id, thumbnail, mb_recording_id, created_at,
                   first_enriched_at, last_enriched_at, genre, mb_year, bpm, energy, danceability
            FROM tracks
            UNION ALL
            SELECT 'discovery', NULL, rowid, identity_key, video_id, title, artist,
                   source_browse_id, thumbnail, NULL, found_at,
                   last_enriched, last_enriched, genre, year, bpm, energy, danceability
            FROM discovered_tracks
        )
    """

    @staticmethod
    def _queue_track(row):
        # The waterfall uses a library id or a discovery identity key as its completion token.
        # Omit the absent id rather than returning id=None, which would mask the identity key.
        return {k: row[k] for k in ("source", "id", "identity_key", "video_id", "title", "artist",
                                    "thumbnail", "mb_recording_id")
                if k != "id" or row[k] is not None}

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
    def next_enrich_batch(self, limit, offset=0) -> list:
        """One priority queue across library and discovery tracks, ordered by tier:
        (0) new arrivals (created after the worker last caught up), (1) played songs by playcount,
        (2) zero-play songs in a played playlist/album/artist by that container's plays, (3) orphans."""
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key='enrich_caught_up_at'").fetchone()
        caught_up = float(row["value"]) if row and row["value"] else 0.0
        rows = self.conn.execute(self._QUEUE_CTES + ", " + self._TRACKS_CTE + """,
          queue AS (
            SELECT t.*, COALESCE(p.c, 0) sp,
              MAX(COALESCE((SELECT MAX(plp.c) FROM playlist_tracks pt JOIN pl_plays plp
                            ON plp.playlist_id=pt.playlist_id WHERE pt.track_id=t.id), 0),
                  COALESCE((SELECT c FROM al_plays WHERE ab=t.album_browse_id), 0),
                  COALESCE((SELECT c FROM ar_plays WHERE ar=t.artist), 0)) AS best
            FROM enrichment_tracks t LEFT JOIN plays p ON p.identity_key=t.identity_key
            WHERE t.first_enriched_at IS NULL
          )
            SELECT * FROM queue
            ORDER BY
              CASE WHEN created_at IS NOT NULL AND created_at > ? THEN 0
                   WHEN sp > 0 THEN 1
                   WHEN best > 0 THEN 2 ELSE 3 END ASC,
              CASE WHEN created_at IS NOT NULL AND created_at > ? THEN -created_at
                   WHEN sp > 0 THEN -sp ELSE -best END ASC,
              created_at DESC, queue_order ASC, source ASC
            LIMIT ? OFFSET ?""", (caught_up, caught_up, limit, offset)).fetchall()
        return [self._queue_track(r) for r in rows]

    @synchronized
    def resweep_batch(self, limit, stale_before) -> list:
        """Processed library or discovery tracks that are stale AND still missing
        a core field, re-attempted only when the primary queue is empty. Oldest-checked first."""
        rows = self.conn.execute(
            "WITH " + self._TRACKS_CTE + " SELECT * FROM enrichment_tracks "
            "WHERE first_enriched_at IS NOT NULL AND last_enriched_at < ? "
            "AND (genre IS NULL OR genre='' OR mb_year IS NULL OR mb_year='' "
            "     OR bpm IS NULL OR energy IS NULL OR danceability IS NULL) "
            "ORDER BY last_enriched_at ASC, queue_order ASC, source ASC LIMIT ?",
            (stale_before, limit)).fetchall()
        return [self._queue_track(r) for r in rows]

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
    def requeue_enrichment(self, track_ids) -> None:
        """Put explicitly requested incomplete tracks back on the worker's primary queue.

        A prior provider miss is normally retried only by the slow stale sweep. Adding a genreless
        suggestion is fresh user intent, so it deserves an immediate retry even when that library
        track was attempted before.
        """
        track_ids = list(dict.fromkeys(track_ids))
        if not track_ids:
            return
        qs = ",".join("?" * len(track_ids))
        self.conn.execute(
            f"UPDATE tracks SET first_enriched_at=NULL, last_enriched_at=NULL WHERE id IN ({qs})",
            track_ids)
        self.conn.commit()

    @synchronized
    def coverage_stats(self, include_discovery=False) -> dict:
        # Onboarding still measures the library. The enrichment page measures every queued source.
        prefix = "WITH " + self._TRACKS_CTE if include_discovery else ""
        table = "enrichment_tracks" if include_discovery else "tracks"
        r = self.conn.execute(
            prefix + " SELECT COUNT(*) total, "
            "  SUM(first_enriched_at IS NOT NULL) processed, "
            "  SUM(genre IS NOT NULL AND genre<>'') genre, "
            "  SUM(mb_year IS NOT NULL AND mb_year<>'') year, "
            "  SUM(bpm IS NOT NULL) bpm, SUM(energy IS NOT NULL) energy, "
            f"  SUM(danceability IS NOT NULL) danceability FROM {table}").fetchone()
        return {k: (r[k] or 0) for k in
                ("total", "processed", "genre", "year", "bpm", "energy", "danceability")}

    @synchronized
    def queue_remaining(self) -> int:
        return self.conn.execute(
            "WITH " + self._TRACKS_CTE +
            " SELECT COUNT(*) n FROM enrichment_tracks WHERE first_enriched_at IS NULL").fetchone()["n"]

    @synchronized
    def outstanding_conflicts(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM enrichment_conflict WHERE resolved=0").fetchone()["n"]

    @synchronized
    def enrichment_activity_stats(self, now) -> dict:
        row = self.conn.execute(
            "WITH " + self._TRACKS_CTE +
            " SELECT MAX(last_enriched_at) latest, SUM(last_enriched_at >= ?) recent "
            "FROM enrichment_tracks", (now - 900,)).fetchone()
        return {"latest": row["latest"], "recent": row["recent"] or 0}

    @synchronized
    def enriched_tracks(self, limit=40, offset=0) -> list:
        """Completed provider passes, newest first. Missing metadata does not imply a failed pass."""
        rows = self.conn.execute(
            "WITH " + self._TRACKS_CTE +
            " SELECT id, title, artist, genre, mb_year, bpm, energy, danceability, last_enriched_at "
            "FROM enrichment_tracks WHERE first_enriched_at IS NOT NULL "
            "ORDER BY last_enriched_at DESC, queue_order DESC, source ASC LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()
        return [dict(r) for r in rows]

    @synchronized
    def unresolved_enrichment_conflicts(self, limit=20, offset=0) -> list:
        """Library-wide review, paged by field disagreement rather than by song."""
        rows = self.conn.execute(
            self._review_select() + " WHERE c.resolved=0 "
            "ORDER BY t.title COLLATE NOCASE, t.id, c.field LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()
        return self._review_conflicts(rows)

    @staticmethod
    def _review_select():
        columns = ", ".join("t." + col for col in sorted({spec[0] for spec in _FIELD_COLUMN.values()}))
        return ("SELECT c.track_id, c.field, c.candidates, c.resolved_value, "
                "t.title, t.artist, t.video_id, t.mb_recording_id, "
                f"{columns} FROM enrichment_conflict c JOIN tracks t ON t.id=c.track_id")

    def _review_conflicts(self, rows):
        """Always offer the saved value, even if it is absent from today's provider candidates.

        Provider matches describe retained evidence, not proof of which write originally won.
        Older/custom values without provenance are explicitly labelled as such in the UI.
        """
        result = self._conflicts(rows)
        for conflict, row in zip(result, rows):
            spec = _FIELD_COLUMN.get(conflict["field"])
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
                    option = {"value": str(value), "label": value, "current": False,
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
            if row["resolved_value"] is not None and key(row["resolved_value"]) == current_key:
                options[0]["source_note"] = "Your choice"
            elif not options[0]["providers"]:
                options[0]["source_note"] = ("No saved value" if current_key is None else
                                              "Saved locally; original source not recorded")
            for option in options:
                option["id"] = (f"enrich-choice-{conflict['track_id']}-{conflict['field']}-"
                                + option["value"].encode().hex())
            # Keep alternatives in the same positions after a save, including keyboard focus.
            provider_order = {}
            for candidate in conflict["candidates"]:
                provider_order.setdefault(key(candidate["value"]), len(provider_order))
            options.sort(key=lambda option: provider_order.get(key(option["value"]), -1))
            conflict["options"] = options
        return result

    @synchronized
    def enrichment_conflict_detail(self, track_id, field) -> dict | None:
        rows = self.conn.execute(self._review_select() + " WHERE c.track_id=? AND c.field=?",
                                 (track_id, field)).fetchall()
        return self._review_conflicts(rows)[0] if rows else None

    @synchronized
    def resolve_enrichment_candidate(self, track_id, field, value, now=None) -> bool:
        """Accept a provider candidate or the current saved value. Repeated choices are safe."""
        spec = _FIELD_COLUMN.get(field)
        if spec is None:
            return False
        row = self.conn.execute(
            f"SELECT c.candidates, t.{spec[0]} current_value FROM enrichment_conflict c "
            "JOIN tracks t ON t.id=c.track_id WHERE c.track_id=? AND c.field=?",
            (track_id, field)).fetchone()
        if row is None:
            return False
        offered = {str(c["value"]) for c in json.loads(row["candidates"])}
        offered.add(str(row["current_value"]) if row["current_value"] is not None else "")
        if value not in offered:
            return False
        self.resolve_conflict(track_id, field, value, now=now)
        return True

    @synchronized
    def processed_timeline(self, buckets=40) -> list:
        """Cumulative processed-track count over time, as up to `buckets` (t, cumulative) points,
        for the trend sparkline. Empty list when nothing's been processed yet."""
        rng = self.conn.execute(
            "WITH " + self._TRACKS_CTE +
            " SELECT MIN(first_enriched_at) lo, MAX(first_enriched_at) hi, COUNT(*) n "
            "FROM enrichment_tracks WHERE first_enriched_at IS NOT NULL").fetchone()
        if not rng["n"] or rng["lo"] is None:
            return []
        lo, hi, total = rng["lo"], rng["hi"], rng["n"]
        if hi <= lo:
            return [{"t": hi, "n": total}]
        width = (hi - lo) / buckets
        out, cum, i = [], 0, 0
        rows = self.conn.execute(
            "WITH " + self._TRACKS_CTE +
            " SELECT first_enriched_at e FROM enrichment_tracks WHERE first_enriched_at IS NOT NULL "
            "ORDER BY first_enriched_at").fetchall()
        for b in range(1, buckets + 1):
            edge = lo + width * b
            while i < len(rows) and rows[i]["e"] <= edge:
                cum += 1
                i += 1
            out.append({"t": edge, "n": cum})
        out[-1]["n"] = total
        return out
