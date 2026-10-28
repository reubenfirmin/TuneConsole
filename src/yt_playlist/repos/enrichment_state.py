"""Provider attempts, configuration, cooldowns, retained evidence and artwork retries.

Writes own their transaction on the Store connection. Completing a provider refresh also
reselects genre and clears its job in that transaction; metadata policy lives in core.metadata.
"""
import json
import time
from contextlib import contextmanager

from yt_playlist.core import catalog, metadata, metadata_observations
from yt_playlist.core.db_diagnostics import database_operation
from yt_playlist.repos.base import Repo, snapshot_read, synchronized

REFRESH_KEY = 'enrich_provider_refresh:'
RECONCILE_BATCH = 64


class EnrichmentStateRepo(Repo):
    @contextmanager
    def subject_guard(self, entry_id, recording_id):
        """Publish a complete result/attempt before another thread can reassociate its entry.

        Provider requests run outside this scope. Nested repository operations
        share the serving lock; no network call may run while it is held.
        """
        with self._lock:
            entry_id = catalog.resolve_entry(self.conn, entry_id)
            subject = self.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
            if subject is None or subject[0] != recording_id:
                raise metadata_observations.SubjectChanged('Enrichment catalog subject changed during lookup')
            yield

    @synchronized
    def record_metadata_observations(self, entry_id, provider, observations, *, run_id, now=None,
                                     expected_recording_id=None):
        """Retain source observations without applying them as catalog overrides."""
        entry_id = catalog.resolve_entry(self.conn, entry_id)
        with self.conn:
            metadata_observations.record(self.conn, entry_id, provider, observations,
                                         run_id=run_id, now=time.time() if now is None else now,
                                         expected_recording_id=expected_recording_id)

    @synchronized
    def metadata_observations(self, identity_key, *, after_id=0, limit=100):
        """One recording's evidence in bounded, restartable ID order; legacy rows stay separate."""
        if type(limit) is not int or not 1 <= limit <= 512:
            raise ValueError('Observation page size must be between 1 and 512')
        if type(after_id) is not int or after_id < 0:
            raise ValueError('Invalid observation cursor')
        key = catalog.resolve_key(self.conn, identity_key)
        rows = self.conn.execute(
            'SELECT f.* FROM metadata_findings f WHERE f.recording_id='
            '(SELECT id FROM recordings WHERE identity_key=?) AND f.id>? '
            'AND f.provenance_json IS NOT NULL ORDER BY f.id LIMIT ?', (key, after_id, limit))
        return [{**dict(row), 'value': json.loads(row['value_json']),
                 'provenance': json.loads(row['provenance_json'])} for row in rows]

    @synchronized
    def integration_cooldown(self, provider):
        row = self.conn.execute('SELECT * FROM enrichment_provider_cooldowns WHERE provider=?',
                                (provider,)).fetchone()
        return dict(row) if row else None

    @synchronized
    def defer_integration(self, provider, *, now, retry_after, error):
        """An IAO publishes a host outage before any dependent caller can retry it."""
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            old = self.integration_cooldown(provider)
            failures = (old['failures'] if old else 0) + 1
            deadline = max(retry_after, now + min(3600, 30 * 2 ** min(failures - 1, 7)),
                           old['retry_at'] if old else 0)
            self.conn.execute('INSERT OR REPLACE INTO enrichment_provider_cooldowns VALUES (?,?,?,?)',
                              (provider, failures, deadline, error))
        return dict(provider=provider, failures=failures, retry_at=deadline, error=error)

    @synchronized
    def clear_integration_cooldown(self, provider, admitted_deadline):
        """A completed request cannot erase a newer outage from another process."""
        with self.conn:
            self.conn.execute('DELETE FROM enrichment_provider_cooldowns WHERE provider=? AND retry_at<=?',
                              (provider, admitted_deadline))

    @synchronized
    def record_enrichment_attempt(self, identity_key, provider, fields, *, retry=False, now=None,
                                  video_id=None, error="", cooldown=False, retry_after=0, providers=(),
                                  manage_cooldown=True):
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
            if cooldown and manage_cooldown:
                old = self.conn.execute("SELECT * FROM enrichment_provider_cooldowns WHERE provider=?",
                                        (provider,)).fetchone()
                count = (old["failures"] if old else 0) + 1
                deadline = max(deadline, result["at"] + min(3600, 30 * 2 ** min(count - 1, 7)),
                               old["retry_at"] if old else 0)
                self.conn.execute("INSERT OR REPLACE INTO enrichment_provider_cooldowns VALUES (?,?,?,?)",
                                  (provider,count,deadline,error))
            elif not retry and manage_cooldown:
                self.conn.execute("DELETE FROM enrichment_provider_cooldowns WHERE provider=?", (provider,))
            if not retry:
                refresh = self.conn.execute("SELECT 1 FROM enrichment_provider_jobs "
                    "WHERE recording_id=? AND provider=?", (row['id'], provider)).fetchone()
                if refresh:
                    # Reconsider completed answers after each provider: a replacement must not
                    # stay blocked by an old artist fallback retained before the policy refresh.
                    answers = {r['provider']: json.loads(r['fields_json']).get('genre')
                        for r in self.conn.execute("SELECT s.provider,s.fields_json FROM "
                            "enrichment_provider_state s WHERE s.recording_id=? AND s.status<>'retry' "
                            "AND NOT EXISTS (SELECT 1 FROM enrichment_provider_jobs j "
                            "WHERE j.recording_id=s.recording_id AND j.provider=s.provider)", (row['id'],))}
                    answers[provider] = fields.get('genre')
                    if not providers:
                        from yt_playlist.providers.enrichment import load_config
                        providers = [p['name'] for p in load_config(self._store) if p['enabled']]
                    metadata.refresh_genre(self.conn, row['id'], answers, providers)
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

    def reconcile_provider_configuration(self, configuration):
        """Catch up on a reader; refresh changed providers in restartable bounded writes."""
        with database_operation('EnrichmentStateRepo.reconcile_provider_configuration'):
            self._configure_providers(configuration)
            for provider, current in configuration.items():
                if current['status'] != 'available':
                    continue
                while self._advance_provider_refresh(provider, current):
                    pass
                candidates = self._missing_provider_coverage(provider)
                for start in range(0, len(candidates), RECONCILE_BATCH):
                    if not self._admit_provider_coverage(provider, current,
                                                       candidates[start:start + RECONCILE_BATCH]):
                        break

    def _provider_matches(self, provider, current):
        row = self.conn.execute('SELECT fingerprint,status FROM enrichment_provider_config WHERE provider=?',
                                (provider,)).fetchone()
        return row is not None and tuple(row) == (current['fingerprint'], current['status'])

    @synchronized
    def _configure_providers(self, configuration):
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            for provider, current in configuration.items():
                previous = self.conn.execute("SELECT * FROM enrichment_provider_config WHERE provider=?",
                                             (provider,)).fetchone()
                changed = previous is not None and (previous["fingerprint"] != current["fingerprint"]
                          or previous["status"] != "available" and current["status"] == "available")
                if previous is None or (previous['fingerprint'], previous['status']) != (
                        current['fingerprint'], current['status']):
                    self.conn.execute("INSERT OR REPLACE INTO enrichment_provider_config VALUES (?,?,?)",
                                      (provider,current["fingerprint"],current["status"]))
                if current["status"] != "available":
                    self.conn.execute('DELETE FROM settings WHERE key=?', (REFRESH_KEY + provider,))
                    continue
                if changed:
                    self.conn.execute("DELETE FROM enrichment_provider_cooldowns WHERE provider=?", (provider,))
                    # Save the frontier with the configuration. A restart must not
                    # mistake an incompletely scheduled revision for an unchanged one.
                    cutoff = self.conn.execute('SELECT COALESCE(MAX(id),0) FROM provider_attempts').fetchone()[0]
                    refresh = json.dumps(dict(cursor=0, cutoff=cutoff, fingerprint=current['fingerprint']))
                    self.conn.execute('INSERT INTO settings(key,value) VALUES (?,?) '
                        'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (REFRESH_KEY + provider, refresh))

    @synchronized
    def _advance_provider_refresh(self, provider, current):
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            if not self._provider_matches(provider, current):
                return False
            row = self.conn.execute('SELECT value FROM settings WHERE key=?', (REFRESH_KEY + provider,)).fetchone()
            if row is None:
                return False
            refresh = json.loads(row[0])
            if refresh['fingerprint'] != current['fingerprint']:
                raise RuntimeError('Provider refresh does not match its configuration')
            rows = self.conn.execute('SELECT id FROM recordings WHERE id>? ORDER BY id LIMIT ?',
                                     (refresh['cursor'], RECONCILE_BATCH)).fetchall()
            if not rows:
                self.conn.execute('DELETE FROM settings WHERE key=?', (REFRESH_KEY + provider,))
                return False
            params = dict(ids=json.dumps([r[0] for r in rows]), provider=provider, cutoff=refresh['cutoff'])
            # Attempts after this configuration change may already have completed
            # or established a retry. Never erase that newer state or its cooldown.
            self.conn.execute('DELETE FROM enrichment_provider_state WHERE provider=:provider '
                'AND recording_id IN (SELECT value FROM json_each(:ids)) AND NOT EXISTS ('
                'SELECT 1 FROM provider_attempts p WHERE p.recording_id=enrichment_provider_state.recording_id '
                'AND p.provider=:provider AND p.id>:cutoff)', params)
            # Jobs for temporarily absent recordings remain dormant until membership
            # returns. This also avoids losing refresh intent across membership changes.
            self.conn.execute('INSERT OR IGNORE INTO enrichment_provider_jobs '
                'SELECT id,:provider FROM recordings r WHERE id IN (SELECT value FROM json_each(:ids)) '
                'AND first_enriched_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM provider_attempts p '
                "WHERE p.recording_id=r.id AND p.provider=:provider AND p.id>:cutoff AND p.status<>'retry')", params)
            refresh['cursor'] = rows[-1][0]
            self.conn.execute('UPDATE settings SET value=? WHERE key=?',
                              (json.dumps(refresh), REFRESH_KEY + provider))
        return True

    @snapshot_read
    def _missing_provider_coverage(self, provider):
        return [row[0] for row in self.conn.execute(
            'SELECT r.id FROM recordings r WHERE first_enriched_at IS NOT NULL '
            'AND EXISTS (SELECT 1 FROM provider_attempts p WHERE p.recording_id=r.id) '
            'AND NOT EXISTS (SELECT 1 FROM provider_attempts p WHERE p.recording_id=r.id '
            "AND p.provider=? AND p.status<>'retry') AND NOT EXISTS (SELECT 1 FROM enrichment_provider_jobs j "
            'WHERE j.recording_id=r.id AND j.provider=?) AND ('
            'EXISTS (SELECT 1 FROM catalog_entries e JOIN library_memberships l ON l.entry_id=e.id '
            'WHERE e.recording_id=r.id) OR EXISTS (SELECT 1 FROM discovery_memberships d '
            'WHERE d.recording_id=r.id))', (provider, provider))]

    @synchronized
    def _admit_provider_coverage(self, provider, current, candidates):
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            if not self._provider_matches(provider, current):
                return False
            self.conn.execute('INSERT OR IGNORE INTO enrichment_provider_jobs '
                'SELECT t.recording_id,:provider FROM json_each(:ids) selected '
                'JOIN catalog_enrichment_tracks t ON t.queue_order=selected.value AND t.recording_id=t.queue_order '
                'WHERE t.first_enriched_at IS NOT NULL '
                'AND EXISTS (SELECT 1 FROM provider_attempts p WHERE p.recording_id=t.recording_id) '
                'AND NOT EXISTS (SELECT 1 FROM provider_attempts p WHERE p.recording_id=t.recording_id '
                "AND p.provider=:provider AND p.status<>'retry')",
                dict(ids=json.dumps(candidates), provider=provider))
        return True

    @synchronized
    def enrichment_provider_jobs(self, recording_id):
        return [r[0] for r in self.conn.execute(
            "SELECT j.provider FROM enrichment_provider_jobs j JOIN enrichment_provider_config c "
            "ON c.provider=j.provider WHERE j.recording_id=? AND c.status='available'", (recording_id,))]

    @synchronized
    def provider_coverage(self, recording_id):
        """Per-subject configuration, pending work and actual outcomes."""
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
