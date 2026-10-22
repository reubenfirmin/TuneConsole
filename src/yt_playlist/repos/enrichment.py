"""EnrichmentRepo: the parseable per-provider response log and the derived disagreement records.

The waterfall harness writes one `enrichment_log` row per (run, track, provider, field) and, when
providers disagree on a field, upserts an `enrichment_conflict` (one per track+field). The playlist /
album UI reads the conflict counts (for the header icon) and the unresolved list (for the resolver);
resolving overwrites the canonical track column and marks the conflict resolved.
"""
import json
import time

from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.core import catalog, metadata

# field concept name -> (tracks column, caster). The whitelist that makes a user-chosen resolution
# safe to write straight into the canonical column.
_FIELD_COLUMN = {
    "title": ("title", str), "artist": ("artist", str),
    "genre": ("genre", str), "year": ("mb_year", str), "label": ("label", str),
    "music_key": ("music_key", str), "music_scale": ("music_scale", str),
    "bpm": ("bpm", float), "energy": ("energy", float), "danceability": ("danceability", float),
    "popularity": ("popularity", int), "gain": ("gain", float),
}


class EnrichmentRepo(Repo):
    @synchronized
    def apply_provider_fields(self, track_id, provider, fields, *, providers=()):
        """Select typed provider findings below explicit user overrides."""
        track_id = catalog.resolve_entry(self.conn,track_id)
        with self.conn:
            metadata.apply_provider(self.conn,track_id,provider,fields,providers)

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
    def replace_catalog_hints(self, track_id, fields):
        """A direct provider answer can supersede an imported album/radio hint.

        Selected user choices remain item-scoped. Provider-selected and unknown legacy values
        continue to use the fill-only policy; every superseded hint remains retained as evidence.
        """
        with self.conn:
            catalog.replace_hints(self.conn,track_id,fields)

    @synchronized
    def record_enrichment_attempt(self, identity_key, provider, fields, *, retry=False, now=None,
                                  video_id=None, error="", cooldown=False, retry_after=0):
        key = catalog.resolve_key(self.conn, identity_key, video_id)
        row = self.conn.execute("SELECT id,COALESCE(last_enriched_at,-1) cycle FROM recordings WHERE identity_key=?", (key,)).fetchone()
        result = {"at": time.time() if now is None else now,
                  "status": "retry" if retry else ("found" if fields else "no_match"), "fields": fields}
        if row is None:
            self.conn.execute("INSERT OR REPLACE INTO legacy_enrichment_attempts VALUES (?,?,?)",
                              (identity_key,json.dumps({provider:result}),"unresolved input identity"))
        else:
            self.conn.execute(
                "INSERT INTO provider_attempts(recording_id,input_video_id,provider,at,status,fields_json) VALUES (?,?,?,?,?,?)",
                (row["id"],video_id or "",provider,result["at"],result["status"],json.dumps(fields)))
            previous = self.conn.execute(
                "SELECT * FROM enrichment_provider_state WHERE recording_id=? AND provider=? AND cycle=?",
                (row["id"], provider, row["cycle"])).fetchone()
            retained = {**(json.loads(previous["fields_json"]) if previous else {}), **fields}
            failures = (previous["failures"] if previous else 0) + 1 if retry else 0
            deadline = max(result["at"] + min(3600, 30 * 2 ** min(failures - 1, 7)),
                           retry_after) if retry else 0
            self.conn.execute(
                "INSERT OR REPLACE INTO enrichment_provider_state VALUES (?,?,?,?,?,?,?,?,?)",
                (row["id"],provider,row["cycle"],result["at"],result["status"],
                 json.dumps(retained),failures,deadline,error))
            if cooldown:
                old = self.conn.execute("SELECT * FROM enrichment_provider_cooldowns WHERE provider=?",
                                        (provider,)).fetchone()
                count = (old["failures"] if old else 0) + 1
                deadline = max(deadline, result["at"] + min(3600, 30 * 2 ** min(count - 1, 7)),
                               old["retry_at"] if old else 0)
                self.conn.execute("INSERT OR REPLACE INTO enrichment_provider_cooldowns VALUES (?,?,?,?)",
                                  (provider,count,deadline,error))
            elif not retry:
                self.conn.execute("DELETE FROM enrichment_provider_cooldowns WHERE provider=?", (provider,))
            if not retry:
                self.conn.execute("DELETE FROM enrichment_provider_jobs WHERE recording_id=? AND provider=?",
                                  (row["id"], provider))
        self.conn.commit()

    @synchronized
    def enrichment_provider_state(self, recording_id, *, include_previous=False):
        rows = self.conn.execute(
            "SELECT s.* FROM enrichment_provider_state s JOIN recordings r ON r.id=s.recording_id "
            "WHERE s.recording_id=? AND (? OR s.cycle=COALESCE(r.last_enriched_at,-1))",
            (recording_id, include_previous)).fetchall()
        return {r["provider"]: {**dict(r), "fields": json.loads(r["fields_json"])} for r in rows}

    @synchronized
    def enrichment_cooldowns(self):
        return {r["provider"]: dict(r) for r in self.conn.execute("SELECT * FROM enrichment_provider_cooldowns")}

    @synchronized
    def enrichment_retry_status(self):
        rows = self.conn.execute(
            "SELECT s.error,MAX(s.retry_at,COALESCE(c.retry_at,0)) retry_at "
            "FROM enrichment_provider_state s JOIN recordings r ON r.id=s.recording_id "
            "LEFT JOIN enrichment_provider_cooldowns c ON c.provider=s.provider "
            "LEFT JOIN enrichment_provider_config cfg ON cfg.provider=s.provider "
            "WHERE s.status='retry' AND s.cycle=COALESCE(r.last_enriched_at,-1) "
            "AND (cfg.provider IS NULL OR cfg.status='available') "
            "ORDER BY retry_at,s.at").fetchall()
        return {"count": len(rows), "retry_at": rows[0]["retry_at"] if rows else None,
                "error": rows[0]["error"] if rows else ""}

    @synchronized
    def record_artwork_result(self, entry_id, release_ids, outcome, now):
        previous = self.conn.execute("SELECT failures FROM enrichment_artwork WHERE entry_id=?",
                                     (entry_id,)).fetchone()
        failures = (previous[0] if previous else 0) + 1 if outcome.status == "retry" else 0
        retry_at = now + max(outcome.retry_in, min(900,60 * 2 ** min(failures-1,4))) if failures else 0
        self.conn.execute("INSERT OR REPLACE INTO enrichment_artwork VALUES (?,?,?,?,?,?)",
                          (entry_id,json.dumps(release_ids),outcome.status,failures,retry_at,outcome.error))
        self.conn.commit()

    @synchronized
    def due_artwork_retries(self, now, limit=1):
        rows = self.conn.execute("SELECT a.* FROM enrichment_artwork a JOIN catalog_entries e ON e.id=a.entry_id "
            "WHERE a.status='retry' AND a.retry_at<=? AND (EXISTS (SELECT 1 FROM library_memberships l "
            "WHERE l.entry_id=e.id) OR EXISTS (SELECT 1 FROM discovery_memberships d "
            "WHERE d.recording_id=e.recording_id)) ORDER BY a.retry_at,a.entry_id LIMIT ?", (now,limit)).fetchall()
        return [{**dict(r), "release_ids": json.loads(r["release_ids_json"])} for r in rows]

    @synchronized
    def artwork_retry_status(self):
        row = self.conn.execute("SELECT COUNT(*) count,MIN(a.retry_at) retry_at FROM enrichment_artwork a "
            "JOIN catalog_entries e ON e.id=a.entry_id WHERE a.status='retry' AND "
            "(EXISTS (SELECT 1 FROM library_memberships l WHERE l.entry_id=e.id) OR "
            "EXISTS (SELECT 1 FROM discovery_memberships d WHERE d.recording_id=e.recording_id))").fetchone()
        return dict(row)

    @staticmethod
    def _eligible_providers(*, coverage=False):
        # An unfinished pass can run if any pending provider is due, or all have already answered
        # (e.g. a process stopped just before stamping completion). Successful answers alone must
        # not keep a track with a deferred provider at the head of the queue.
        pending = """FROM json_each(:providers) p
            LEFT JOIN enrichment_provider_state s ON s.recording_id=t.recording_id
              AND s.provider=p.value AND s.cycle=COALESCE(t.last_enriched_at,-1)
            LEFT JOIN enrichment_provider_cooldowns c ON c.provider=p.value
            WHERE (s.recording_id IS NULL OR s.status='retry')"""
        if coverage:
            pending += " AND (t.first_enriched_at IS NULL OR EXISTS (SELECT 1 FROM " \
                       "enrichment_provider_jobs j WHERE j.recording_id=t.recording_id AND j.provider=p.value))"
        return ("(:providers IS NULL OR EXISTS (SELECT 1 " + pending +
                " AND COALESCE(s.retry_at,0)<=:now AND COALESCE(c.retry_at,0)<=:now) "
                "OR NOT EXISTS (SELECT 1 " + pending + "))")

    @staticmethod
    def _pending_coverage(recording="t.recording_id"):
        return ("EXISTS (SELECT 1 FROM enrichment_provider_jobs j JOIN enrichment_provider_config c "
                "ON c.provider=j.provider WHERE j.recording_id=" + recording + " AND c.status='available')")

    @synchronized
    def reconcile_provider_configuration(self, configuration):
        """Schedule only newly enabled/configured providers; never clear facts or pass timestamps."""
        with self.conn:
            for provider, current in configuration.items():
                previous = self.conn.execute("SELECT * FROM enrichment_provider_config WHERE provider=?",
                                             (provider,)).fetchone()
                changed = previous is not None and (previous["fingerprint"] != current["fingerprint"]
                          or previous["status"] != "available" and current["status"] == "available")
                self.conn.execute("INSERT OR REPLACE INTO enrichment_provider_config VALUES (?,?,?)",
                                  (provider,current["fingerprint"],current["status"]))
                if current["status"] != "available":
                    continue
                if changed:
                    self.conn.execute("DELETE FROM enrichment_provider_state WHERE provider=?", (provider,))
                    self.conn.execute("DELETE FROM enrichment_provider_cooldowns WHERE provider=?", (provider,))
                self.conn.execute(
                    "INSERT OR IGNORE INTO enrichment_provider_jobs "
                    "SELECT recording_id,? FROM catalog_enrichment_tracks t WHERE first_enriched_at IS NOT NULL "
                    "AND (? OR (EXISTS (SELECT 1 FROM provider_attempts p WHERE p.recording_id=t.recording_id) "
                    "AND NOT EXISTS (SELECT 1 FROM provider_attempts p "
                    "WHERE p.recording_id=t.recording_id AND p.provider=? AND p.status<>'retry')))",
                    (provider,changed,provider))

    @synchronized
    def enrichment_provider_jobs(self, recording_id):
        return [r[0] for r in self.conn.execute(
            "SELECT j.provider FROM enrichment_provider_jobs j JOIN enrichment_provider_config c "
            "ON c.provider=j.provider WHERE j.recording_id=? AND c.status='available'", (recording_id,))]

    @synchronized
    def provider_coverage(self, recording_id):
        """Per-subject coverage distinguishes configuration, pending work and actual outcomes."""
        pending = set(self.enrichment_provider_jobs(recording_id))
        states = self.enrichment_provider_state(recording_id, include_previous=True)
        result = {}
        for row in self.conn.execute("SELECT provider,status FROM enrichment_provider_config"):
            name, status = row["provider"], row["status"]
            state = states.get(name, {})
            result[name] = (status if status != "available" else
                            state.get("status", "pending") if name in pending else
                            state.get("status", "unknown"))
        return result

    @synchronized
    def enrichment_attempts(self, identity_key, video_id=None):
        key = catalog.resolve_key(self.conn,identity_key,video_id)
        rows = self.conn.execute(
            "SELECT p.* FROM provider_attempts p WHERE "
            "(p.recording_id=(SELECT id FROM recordings WHERE identity_key=?) OR "
            "p.input_video_id IN (SELECT y.video_id FROM youtube_items y JOIN recordings r "
            "ON r.id=y.recording_id WHERE r.identity_key=?)) "
            "AND (? IS NULL OR p.input_video_id=?) ORDER BY p.at,p.id",
            (key,key,video_id,video_id)).fetchall()
        return {r["provider"]: {"at":r["at"],"status":r["status"],"fields":json.loads(r["fields_json"])}
                for r in rows}

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
    def log_enrichment(self, track_id, run_id, provider, field, value, now=None) -> None:
        track_id = catalog.resolve_entry(self.conn, track_id)
        subject = self.conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?', (track_id,)).fetchone()
        scope = ('release' if field in ('year','label','mb_release_ids','cover_url') else
                 'item' if field in ('popularity','gain','loudness','dynamic_complexity') else 'recording')
        self.conn.execute(
            "INSERT INTO enrichment_log(track_id, run_id, provider, field, value, created_at,recording_id,input_video_id,field_scope) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (track_id, run_id, provider, field,
             None if value is None else str(value), time.time() if now is None else now,
             subject['recording_id'],subject['video_id'],scope))
        if value and field in ('mb_recording_id','isrc'):
            scheme = 'musicbrainz' if field == 'mb_recording_id' else 'isrc'
            values = value if isinstance(value,(list,tuple)) else [value]
            for identifier in values:
                self.conn.execute("INSERT INTO recording_identifiers(recording_id,scheme,entity_type,value,source,input_video_id,status,observed_at) VALUES (?,?, 'recording',?,?,?,'candidate',?)",
                                  (subject['recording_id'],scheme,str(identifier),provider,subject['video_id'],time.time() if now is None else now))
        self.conn.commit()

    @synchronized
    def mark_recordings_enriched(self, recording_ids, now):
        self.conn.executemany('UPDATE recordings SET first_enriched_at=COALESCE(first_enriched_at,?),last_enriched_at=? WHERE id=?',
                              [(now,now,rid) for rid in recording_ids])
        self.conn.commit()

    @synchronized
    def enrichment_memberships(self, recording_ids):
        ids = list(recording_ids)
        if not ids:
            return set()
        placeholders = ','.join('?' for _ in ids)
        scopes = set()
        if self.conn.execute(f'SELECT 1 FROM catalog_entries e JOIN library_memberships l ON l.entry_id=e.id WHERE e.recording_id IN ({placeholders}) LIMIT 1',ids).fetchone():
            scopes.add('library')
        if self.conn.execute(f'SELECT 1 FROM discovery_memberships WHERE recording_id IN ({placeholders}) LIMIT 1',ids).fetchone():
            scopes.add('discovery')
        return scopes

    @synchronized
    def upsert_conflict(self, track_id, field, candidates, now=None, *, refreshed_providers=None) -> None:
        track_id = catalog.resolve_entry(self.conn, track_id)
        """Record (or refresh) a disagreement for one track+field. `candidates` is a list of
        {"provider","value"}. Re-run rule: refresh the candidate list; a previously-resolved conflict
        stays resolved while its candidate *values* are unchanged, but reopens if a new value appears."""
        row = self.conn.execute(
            "SELECT candidates, resolved FROM enrichment_conflict WHERE track_id=? AND field=?",
            (track_id, field)).fetchone()
        if row is not None and refreshed_providers is not None:
            current_providers = {c['provider'] for c in candidates}
            # Partial passes cannot erase a previous provider merely because it was skipped or
            # failed. Otherwise a later two-provider agreement could hide the missing third one.
            candidates = [*candidates, *(c for c in json.loads(row['candidates'])
                if c['provider'] not in refreshed_providers and c['provider'] not in current_providers)]
        payload = json.dumps(candidates)
        new_values = {str(c["value"]) for c in candidates}
        # resolved=2 retires a disagreement after provider convergence, independently of a user
        # choice (resolved=1). Any later disagreement reopens an automatically retired record.
        reopen = row is not None and row["resolved"] == 2
        if row is not None and row["resolved"] == 1:
            old_values = {str(c["value"]) for c in json.loads(row["candidates"])}
            reopen = bool(new_values - old_values)        # a genuinely new option showed up
        resolved_clause = "0" if (row is None or reopen) else "resolved"
        self.conn.execute(
            "INSERT INTO enrichment_conflict(track_id, field, candidates, resolved, updated_at) "
            "VALUES (?,?,?,0,?) "
            "ON CONFLICT(track_id, field) DO UPDATE SET "
            f"  candidates=excluded.candidates, resolved={resolved_clause}, updated_at=excluded.updated_at",
            (track_id, field, payload, now or time.time()))
        self.conn.execute(
            "UPDATE enrichment_conflict SET recording_id=(SELECT recording_id FROM catalog_entries WHERE id=?),"
            "input_video_id=(SELECT video_id FROM catalog_entries WHERE id=?),field_scope=? "
            "WHERE track_id=? AND field=?",
            (track_id,track_id,'release' if field in ('year','label') else
             'item' if field in ('popularity','gain') else 'recording',track_id,field))
        self.conn.commit()

    @synchronized
    def retire_agreed_conflicts(self, track_id, evidence, now=None) -> None:
        """Retire current conflicts only after every previous candidate provider refreshed the field.

        The caller supplies agreeing, nonempty evidence from a fully successful pass. Candidate
        history and explicit selections remain intact; convergence never changes effective metadata.
        Recording facts can converge through another associated input; release/item facts cannot.
        """
        track_id = catalog.resolve_entry(self.conn, track_id)
        subject = self.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?',
                                    (track_id,)).fetchone()
        if subject is None:
            return
        rows = self.conn.execute(
            "SELECT id,field,candidates FROM enrichment_conflict WHERE resolved=0 AND "
            "(track_id=? OR (field_scope='recording' AND recording_id=?))",
            (track_id, subject['recording_id'])).fetchall()
        for row in rows:
            providers = set(evidence.get(row['field'], {}))
            previous = {candidate['provider'] for candidate in json.loads(row['candidates'])}
            if len(providers) >= 2 and previous <= providers:
                self.conn.execute('UPDATE enrichment_conflict SET resolved=2,updated_at=? WHERE id=?',
                                  (time.time() if now is None else now, row['id']))
        self.conn.commit()

    @synchronized
    def _choose_track_field(self, track_id, field, value, *, now=None):
        track_id = catalog.resolve_entry(self.conn, track_id)
        """Overwrite a single canonical track column (used by conflict resolution). Whitelisted."""
        spec = _FIELD_COLUMN.get(field)
        if spec is None:
            raise ValueError(f"not a writable enrichment field: {field!r}")
        col, cast = spec
        try:
            v = cast(value) if value not in (None, "") else ("" if cast is str else None)
        except (TypeError, ValueError):
            v = value
        catalog.choose_field(self.conn,track_id,col,v,'conflict_resolution',now=now)

    @synchronized
    def set_track_field(self, track_id, field, value) -> None:
        with self.conn:
            self._choose_track_field(track_id,field,value)

    @synchronized
    def resolve_conflict(self, track_id, field, value, now=None) -> None:
        track_id = catalog.resolve_entry(self.conn, track_id)
        """Apply the user's choice: overwrite the canonical column and mark the conflict resolved."""
        at = time.time() if now is None else now
        with self.conn:
            self._choose_track_field(track_id,field,value,now=at)
            self.conn.execute(
                "UPDATE enrichment_conflict SET resolved=1, resolved_value=?, updated_at=? "
                "WHERE track_id=? AND field=?",
                (None if value is None else str(value), at, track_id, field))

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

    # --- enrichment worker: priority queue, processed bookkeeping, coverage stats ----------------

    # One representative input per recording, independent of library/discovery overlap.
    _TRACKS_CTE = "enrichment_tracks AS (SELECT * FROM catalog_enrichment_tracks)"

    @staticmethod
    def _queue_track(row):
        # Completion addresses the recording job; id identifies its exact input item/entry.
        return {"job_id": row["recording_id"], **{k: row[k] for k in ("recording_id", "source", "id", "identity_key", "video_id", "title", "artist", "last_enriched_at",
                                    "thumbnail", "mb_recording_id")
                if k != "id" or row[k] is not None}}

    # Per-track song plays + the play sums of the containers it belongs to (playlist / album / artist).
    # Reused by the priority queue. `tp` = track plays; the three container CTEs aggregate over members.
    _QUEUE_CTES = """
        WITH plays AS (SELECT identity_key, COUNT(*) c FROM listening_days GROUP BY identity_key),
        tp AS (SELECT t.id,t.recording_id, COALESCE(p.c,0) sp FROM catalog_tracks t
               LEFT JOIN plays p ON p.identity_key=t.identity_key),
        pl_plays AS (SELECT playlist_id,SUM(sp) c FROM (
                     SELECT DISTINCT pt.playlist_id,tp.recording_id,tp.sp FROM playlist_tracks pt
                     JOIN tp ON tp.id=pt.track_id) GROUP BY playlist_id),
        al_plays AS (SELECT album_id,SUM(sp) c FROM (
                     SELECT DISTINCT at.album_id,tp.recording_id,tp.sp FROM album_tracks at
                     JOIN tp ON tp.id=at.entry_id) GROUP BY album_id),
        ar_plays AS (SELECT ar,SUM(sp) c FROM (
                     SELECT DISTINCT t.artist ar,tp.recording_id,tp.sp FROM catalog_tracks t
                     JOIN tp ON tp.id=t.id WHERE t.artist IS NOT NULL AND t.artist<>'') GROUP BY ar)
    """

    @synchronized
    def next_enrich_batch(self, limit, offset=0, *, providers=None, now=None) -> list:
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
                  COALESCE((SELECT MAX(ap.c) FROM al_plays ap JOIN album_tracks at ON at.album_id=ap.album_id
                            JOIN catalog_entries e ON e.id=at.entry_id WHERE e.recording_id=t.recording_id), 0),
                  COALESCE((SELECT c FROM ar_plays WHERE ar=t.artist), 0)) AS best
            FROM enrichment_tracks t LEFT JOIN plays p ON p.identity_key=t.identity_key
            WHERE (t.first_enriched_at IS NULL OR """ + self._pending_coverage() + ") AND " + self._eligible_providers(coverage=True) + """
          )
            SELECT * FROM queue
            ORDER BY
              COALESCE((SELECT MAX(s.at) FROM enrichment_provider_state s
                WHERE s.recording_id=queue.recording_id AND s.cycle=COALESCE(queue.last_enriched_at,-1)),0),
              CASE WHEN created_at IS NOT NULL AND created_at > :caught_up THEN 0
                   WHEN sp > 0 THEN 1
                   WHEN best > 0 THEN 2 ELSE 3 END ASC,
              CASE WHEN created_at IS NOT NULL AND created_at > :caught_up THEN -created_at
                   WHEN sp > 0 THEN -sp ELSE -best END ASC,
              created_at DESC, queue_order ASC, source ASC
            LIMIT :limit OFFSET :offset""", {"caught_up": caught_up, "limit": limit, "offset": offset,
                "providers": json.dumps(providers) if providers is not None else None,
                "now": time.time() if now is None else now}).fetchall()
        return [self._queue_track(r) for r in rows]

    @synchronized
    def resweep_batch(self, limit, stale_before, *, providers=None, now=None) -> list:
        """Processed library or discovery tracks that are stale AND still missing
        a core field, re-attempted only when the primary queue is empty. Oldest-checked first."""
        rows = self.conn.execute(
            "WITH " + self._TRACKS_CTE + " SELECT * FROM enrichment_tracks t "
            "WHERE first_enriched_at IS NOT NULL AND last_enriched_at < :stale_before "
            "AND (genre IS NULL OR genre='' OR mb_year IS NULL OR mb_year='' "
            "     OR bpm IS NULL OR energy IS NULL OR danceability IS NULL) "
            "AND NOT " + self._pending_coverage() + " "
            "AND " + self._eligible_providers() +
            " ORDER BY COALESCE((SELECT MAX(s.at) FROM enrichment_provider_state s "
            "WHERE s.recording_id=t.recording_id AND s.cycle=COALESCE(t.last_enriched_at,-1)),0), "
            "last_enriched_at ASC, queue_order ASC, source ASC LIMIT :limit",
            {"stale_before": stale_before, "limit": limit,
             "providers": json.dumps(providers) if providers is not None else None,
             "now": time.time() if now is None else now}).fetchall()
        return [self._queue_track(r) for r in rows]

    @synchronized
    def mark_enriched(self, track_ids, now) -> None:
        """Stamp a processed pass: set first_enriched_at once, bump last_enriched_at every time."""
        if not track_ids:
            return
        track_ids = [catalog.resolve_entry(self.conn,t) for t in track_ids]
        qs = ",".join("?" * len(track_ids))
        self.conn.execute(
            f"UPDATE catalog_tracks SET first_enriched_at = COALESCE(first_enriched_at, ?), "
            f"last_enriched_at = ? WHERE id IN ({qs})", [now, now, *track_ids])
        self.conn.commit()

    @synchronized
    def requeue_enrichment(self, track_ids) -> None:
        """Put explicitly requested incomplete tracks back on the worker's primary queue.

        A prior provider miss is normally retried only by the slow stale sweep. Adding a genreless
        suggestion is fresh user intent, so it deserves an immediate retry even when that library
        track was attempted before.
        """
        track_ids = list(dict.fromkeys(catalog.resolve_entry(self.conn,t) for t in track_ids))
        if not track_ids:
            return
        qs = ",".join("?" * len(track_ids))
        self.conn.execute(
            f"UPDATE catalog_tracks SET first_enriched_at=NULL, last_enriched_at=NULL WHERE id IN ({qs})",
            track_ids)
        self.conn.commit()

    @synchronized
    def coverage_stats(self, include_discovery=False) -> dict:
        # Onboarding still measures the library. The enrichment page measures every queued source.
        prefix = "WITH " + self._TRACKS_CTE if include_discovery else ""
        table = "enrichment_tracks" if include_discovery else "tracks"
        r = self.conn.execute(
            prefix + " SELECT COUNT(*) total,SUM(processed) processed,SUM(genre) genre,"
            "SUM(year) year,SUM(bpm) bpm,SUM(energy) energy,SUM(danceability) danceability FROM ("
            "SELECT recording_id,MAX(first_enriched_at IS NOT NULL AND NOT " +
            self._pending_coverage() + ") processed,"
            "MAX(genre IS NOT NULL AND genre<>'') genre,MAX(mb_year IS NOT NULL AND mb_year<>'') year,"
            "MAX(bpm IS NOT NULL) bpm,MAX(energy IS NOT NULL) energy,"
            f"MAX(danceability IS NOT NULL) danceability FROM {table} t GROUP BY recording_id)").fetchone()
        return {k: (r[k] or 0) for k in
                ("total", "processed", "genre", "year", "bpm", "energy", "danceability")}

    @synchronized
    def queue_remaining(self) -> int:
        return self.conn.execute(
            "WITH " + self._TRACKS_CTE +
            " SELECT COUNT(*) n FROM enrichment_tracks t WHERE first_enriched_at IS NULL OR " +
            self._pending_coverage()).fetchone()["n"]

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
            "FROM enrichment_tracks t WHERE first_enriched_at IS NOT NULL AND NOT " + self._pending_coverage() + " "
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
                f"{columns} FROM enrichment_conflict c JOIN catalog_tracks t ON t.id=c.track_id")

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
            # Keep alternatives in the same positions after a save, including keyboard focus.
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
    def resolve_enrichment_candidate(self, track_id, field, value, now=None) -> bool:
        track_id = catalog.resolve_entry(self.conn, track_id)
        """Accept a provider candidate or the current saved value. Repeated choices are safe."""
        spec = _FIELD_COLUMN.get(field)
        if spec is None:
            return False
        row = self.conn.execute(
            f"SELECT c.candidates, t.{spec[0]} current_value FROM enrichment_conflict c "
            "JOIN catalog_tracks t ON t.id=c.track_id WHERE c.track_id=? AND c.field=?",
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
