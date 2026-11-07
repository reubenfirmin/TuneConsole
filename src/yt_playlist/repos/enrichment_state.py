"""SQL state for provider attempts, configuration, cooldowns and artwork retries."""
import json
import time
import uuid
from contextlib import contextmanager

from yt_playlist.core import catalog, metadata, metadata_observations, metadata_native, metadata_attempts, metadata_identifiers, legacy_evidence
from yt_playlist.core import provider_frontiers
from yt_playlist.core import metadata_runtime, provider_queries
from yt_playlist.core.preparation import metadata_destinations, metadata_publication
from yt_playlist.repos.base import Repo, snapshot_read, synchronized

class EnrichmentStateRepo(Repo):
    @contextmanager
    def state_transaction(self):
        with self._lock:
            with self.conn.atomic():
                self.conn.execute('UPDATE enrichment_provider_config SET provider=provider WHERE 0')
                yield

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
    def capture_inputs(self, entry_id):
        entry_id = catalog.resolve_entry(self.conn, entry_id)
        row = self.conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown provider result input')
        return {'entry_id': entry_id, **dict(row), 'attempts': metadata_attempts.available(self.conn),
                'layout': 'runtime' if metadata_runtime.active(self.conn) else
                          'evidence' if metadata_attempts.available(self.conn) else 'legacy'}

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
        if metadata_runtime.active(self.conn):
            return metadata_runtime.native_page(self.conn, key, after_id=after_id, limit=limit)
        if metadata_native.available(self.conn):
            rows = self.conn.execute('SELECT o.id,d.id AS delivery_id,d.provider,d.input_entry_id AS entry_id,'
                'd.input_recording_id AS recording_id,d.observed_at,d.delivery_key,d.raw_byte_size '
                'FROM native_metadata_observations o JOIN metadata_deliveries d ON d.id=o.delivery_id '
                'WHERE d.input_recording_id=(SELECT id FROM recordings WHERE identity_key=?) AND o.id>? '
                'ORDER BY o.id LIMIT ?', (key, after_id, limit)).fetchall()
            if sum(row['raw_byte_size'] for row in rows) > 8 * 1024 * 1024:
                from yt_playlist.core.activity import WorkBudgetExceeded
                raise WorkBudgetExceeded('Native metadata page exceeds byte budget')
            result = []
            for row in rows:
                payload = metadata_native.read(self.conn, row['id'])
                provenance = {name: value for name, value in payload.items() if name not in ('field', 'value')}
                provenance.setdefault('delivery_id', row['delivery_key'])
                result.append({**{name: row[name] for name in ('id', 'delivery_id', 'provider', 'entry_id',
                                                             'recording_id', 'observed_at')},
                               'field': payload['field'], 'value': payload['value'], 'provenance': provenance})
            return result
        recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?',(key,)).fetchone()
        rows = metadata_destinations.native_rows(self.conn,recording=recording[0] if recording else None,after=after_id,limit=limit)
        if rows is not None:
            return [{**row,'value':json.loads(row['value_json']),'provenance':json.loads(row['provenance_json'])} for row in rows]
        rows = self.conn.execute(
            'SELECT f.* FROM '+metadata_publication.read_table(self.conn,'metadata_findings')+' f WHERE f.recording_id='
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
    def store_integration_cooldown(self, result):
        with self.conn.atomic(join=True):
            self.conn.execute('INSERT OR REPLACE INTO enrichment_provider_cooldowns VALUES (?,?,?,?)',
                              tuple(result[key] for key in ('provider', 'failures', 'retry_at', 'error')))

    @synchronized
    def clear_integration_cooldown(self, provider, admitted_deadline):
        """A completed request cannot erase a newer outage from another process."""
        with self.conn:
            self.conn.execute('DELETE FROM enrichment_provider_cooldowns WHERE provider=? AND retry_at<=?',
                              (provider, admitted_deadline))

    @synchronized
    def record_enrichment_attempt(self, identity_key, provider, fields, *, retry=False, now=None,
                                  video_id=None, error="", retry_after=0, providers=(),
                                  run_id=None, delivery_id=None):
        key = catalog.resolve_key(self.conn, identity_key, video_id)
        row = self.conn.execute("SELECT id,COALESCE(last_enriched_at,-1) cycle FROM recordings WHERE identity_key=?", (key,)).fetchone()
        result = {"at": time.time() if now is None else now,
                  "status": "retry" if retry else ("found" if fields else "no_match"), "fields": fields}
        if metadata_runtime.active(self.conn):
            if row is None:
                raise ValueError('Provider attempt needs a current recording')
            with self.conn:
                if not self.conn.in_transaction:
                    self.conn.execute('BEGIN IMMEDIATE')
                entry = self.conn.execute('SELECT id FROM catalog_entries WHERE recording_id=? '
                    'AND (? IS NULL OR video_id=?) ORDER BY id LIMIT 1', (row['id'], video_id, video_id)).fetchone()
                if isinstance(delivery_id, metadata_runtime.Capture):
                    entry = (delivery_id.entry,)
                if entry is None:
                    raise ValueError('Provider attempt needs a current catalog input')
                if metadata_runtime.record_attempt(self.conn, entry[0], provider, fields, cycle=row['cycle'],
                        retry=retry, now=result['at'], retry_after=retry_after, error=error,
                        run_id=run_id, delivery_id=delivery_id):
                    self._attempt_effects(row['id'], provider, fields, retry=retry, at=result['at'],
                        providers=providers)
            return
        if row is not None and metadata_attempts.available(self.conn):
            with self.conn:
                if not self.conn.in_transaction:
                    self.conn.execute('BEGIN IMMEDIATE')
                entry = self.conn.execute('SELECT id FROM catalog_entries WHERE recording_id=? '
                    'AND (? IS NULL OR video_id=?) ORDER BY id LIMIT 1', (row['id'], video_id, video_id)).fetchone()
                if entry is None:
                    raise ValueError('Provider attempt needs a current catalog input')
                _, inserted = metadata_attempts.record(self.conn, entry[0], provider, fields,
                    cycle=row['cycle'], retry=retry, now=result['at'], retry_after=retry_after,
                    error=error, run_id=run_id, delivery_id=delivery_id)
                if inserted:
                    self._attempt_effects(row['id'], provider, fields, retry=retry, at=result['at'],
                        providers=providers)
            return
        if row is None:
            if legacy_evidence.available(self.conn):
                if not self.conn.in_transaction:
                    self.conn.execute('BEGIN IMMEDIATE')
                legacy_evidence.retain(self.conn, 'enrichment_unresolved_attempt', uuid.uuid4().hex,
                    json.dumps({'identity_key': identity_key, 'video_id': video_id, 'provider': provider, **result}),
                    'unresolved input identity', domain='metadata')
            else:
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
            self._attempt_effects(row['id'], provider, fields, retry=retry, at=result['at'],
                providers=providers)
        self.conn.commit()

    def _attempt_effects(self, recording_id, provider, fields, *, retry, at, providers):
        if not retry:
            refresh = self.conn.execute('SELECT 1 FROM enrichment_provider_jobs WHERE recording_id=? AND provider=?',
                                        (recording_id, provider)).fetchone()
            if refresh:
                if metadata_runtime.active(self.conn):
                    answers = {r['provider']: metadata_runtime.alternatives(self.conn, recording_id,
                        r['provider'], now=at).get('fields', {}).get('genre') for r in self.conn.execute(
                        "SELECT s.provider FROM enrichment_provider_state s WHERE s.recording_id=? AND s.status<>'retry' "
                        'AND NOT EXISTS(SELECT 1 FROM enrichment_provider_jobs j '
                        'WHERE j.recording_id=s.recording_id AND j.provider=s.provider)', (recording_id,))}
                elif metadata_attempts.available(self.conn):
                    answers = metadata_attempts.completed_genres(self.conn, recording_id)
                else:
                    answers = {r['provider']: json.loads(r['fields_json']).get('genre') for r in self.conn.execute(
                        "SELECT s.provider,s.fields_json FROM enrichment_provider_state s WHERE s.recording_id=? AND s.status<>'retry' "
                        'AND NOT EXISTS(SELECT 1 FROM enrichment_provider_jobs j '
                        'WHERE j.recording_id=s.recording_id AND j.provider=s.provider)', (recording_id,))}
                answers[provider] = fields.get('genre')
                if not providers:
                    from yt_playlist.providers.enrichment import load_config
                    providers = [p['name'] for p in load_config(self._store) if p['enabled']]
                metadata.refresh_genre(self.conn, recording_id, answers, providers)
            self.conn.execute('DELETE FROM enrichment_provider_jobs WHERE recording_id=? AND provider=?', (recording_id, provider))

    @synchronized
    def enrichment_provider_state(self, recording_id, *, include_previous=False, entry_id=None):
        if metadata_runtime.active(self.conn):
            result = {}
            for row in self.conn.execute('SELECT s.* FROM enrichment_provider_state s JOIN recordings r '
                    'ON r.id=s.recording_id WHERE s.recording_id=? AND (? OR s.cycle=COALESCE(r.last_enriched_at,-1))',
                    (recording_id, include_previous)):
                data = metadata_runtime.alternatives(self.conn, recording_id, row['provider'])
                fields = metadata_runtime.scoped_fields(self.conn, entry_id, data) if entry_id is not None else data.get('fields', {})
                result[row['provider']] = {**dict(row), 'fields': fields, 'error': data.get('error', '')}
            return result

        rows = self.conn.execute(
            f"SELECT s.* FROM {metadata_publication.read_table(self.conn,'enrichment_provider_state')} s JOIN recordings r ON r.id=s.recording_id "
            "WHERE s.recording_id=? AND (? OR s.cycle=COALESCE(r.last_enriched_at,-1))",
            (recording_id, include_previous)).fetchall()
        typed = metadata_attempts.available(self.conn)
        result = {}
        for row in rows:
            fields = metadata_attempts.state_fields(self.conn,recording_id,row['provider']) if typed else metadata_publication.fields(
                self.conn,'enrichment_provider_state',json.dumps([recording_id,row['provider']],separators=(',',':')))
            result[row['provider']] = {**dict(row),'fields':fields if fields is not None else json.loads(row['fields_json'])}
        return result

    @synchronized
    def expire_provider_alternatives(self, now=None):
        if not metadata_runtime.active(self.conn):
            return 0
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            return metadata_runtime.expire(self.conn, now=now)

    @synchronized
    def enrichment_cooldowns(self):
        return {r["provider"]: dict(r) for r in self.conn.execute("SELECT * FROM enrichment_provider_cooldowns")}

    @synchronized
    def enrichment_retry_status(self):
        rows = self.conn.execute(
            "SELECT s.error,MAX(s.retry_at,COALESCE(c.retry_at,0)) retry_at "
            f"FROM {metadata_publication.read_table(self.conn,'enrichment_provider_state')} s JOIN recordings r ON r.id=s.recording_id "
            "LEFT JOIN enrichment_provider_cooldowns c ON c.provider=s.provider "
            "LEFT JOIN enrichment_provider_config cfg ON cfg.provider=s.provider "
            "WHERE s.status='retry' AND s.cycle=COALESCE(r.last_enriched_at,-1) "
            "AND (cfg.provider IS NULL OR cfg.status='available') "
            "AND (s.provider<>'lastfm' OR EXISTS (SELECT 1 FROM catalog_enrichment_tracks t "
            "WHERE t.recording_id=r.id AND " + provider_queries.lastfm_ready_sql() + ")) "
            "ORDER BY retry_at,s.at").fetchall()
        return {"count": len(rows), "retry_at": rows[0]["retry_at"] if rows else None,
                "error": rows[0]["error"] if rows else ""}

    @synchronized
    def artwork_state(self, entry_id):
        previous = self.conn.execute("SELECT failures FROM enrichment_artwork WHERE entry_id=?",
                                     (entry_id,)).fetchone()
        return dict(previous) if previous else None

    @synchronized
    def store_artwork_result(self, entry_id, release_ids, result):
        with self.conn.atomic(join=True):
            self.conn.execute('INSERT OR REPLACE INTO enrichment_artwork VALUES (?,?,?,?,?,?)',
                (entry_id, json.dumps(release_ids), *(result[key] for key in ('status', 'failures', 'retry_at', 'error'))))

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

    def _provider_matches(self, provider, current):
        row = self.conn.execute('SELECT fingerprint,status FROM enrichment_provider_config WHERE provider=?',
                                (provider,)).fetchone()
        return row is not None and tuple(row) == (current['fingerprint'], current['status'])

    @synchronized
    def configuration_inputs(self, providers):
        runtime = metadata_runtime.active(self.conn)
        return {'runtime': runtime,
                'last_attempt': 0 if runtime else self.conn.execute('SELECT COALESCE(MAX(id),0) FROM provider_attempts').fetchone()[0],
                'configuration': {row['provider']: {'fingerprint': row['fingerprint'], 'status': row['status']}
                                  for row in self.conn.execute('SELECT * FROM enrichment_provider_config')},
                'frontiers': {provider: provider_frontiers.read(self.conn, provider) for provider in providers}}

    @synchronized
    def store_configuration_changes(self, changes, cutoff):
        with self.conn.atomic(join=True):
            for change in changes:
                provider, current = change['provider'], change['current']
                if change['replace']:
                    self.conn.execute("INSERT INTO enrichment_provider_config VALUES (?,?,?) "
                                      "ON CONFLICT(provider) DO UPDATE SET fingerprint=excluded.fingerprint,status=excluded.status",
                                      (provider,current["fingerprint"],current["status"]))
                if change['clear']:
                    provider_frontiers.clear(self.conn, provider)
                if change['restart']:
                    self.conn.execute("DELETE FROM enrichment_provider_cooldowns WHERE provider=?", (provider,))
                    provider_frontiers.start(self.conn, provider, cutoff, current['fingerprint'])

    @synchronized
    def refresh_inputs(self, provider, limit):
        refresh = provider_frontiers.read(self.conn, provider)
        configuration = self.conn.execute('SELECT fingerprint,status FROM enrichment_provider_config WHERE provider=?', (provider,)).fetchone()
        runtime = metadata_runtime.active(self.conn)
        rows = []
        if refresh is not None:
            attempts = ('0 last_attempt,0 completed_attempt' if runtime else
                '(SELECT COALESCE(MAX(p.id),0) FROM provider_attempts p WHERE p.recording_id=r.id AND p.provider=:provider) last_attempt,'
                '(SELECT COALESCE(MAX(p.id),0) FROM provider_attempts p WHERE p.recording_id=r.id AND p.provider=:provider '
                "AND p.status<>'retry') completed_attempt")
            rows = [dict(row) for row in self.conn.execute('SELECT r.id,r.first_enriched_at,s.at,s.status,' + attempts +
                ' FROM recordings r LEFT JOIN enrichment_provider_state s ON s.recording_id=r.id AND s.provider=:provider '
                'WHERE r.id>:cursor ORDER BY r.id LIMIT :limit', dict(provider=provider, cursor=refresh['cursor'], limit=limit))]
        return {'frontier': refresh, 'configuration': dict(configuration) if configuration else None, 'runtime': runtime, 'rows': rows}

    @synchronized
    def clear_provider_refresh(self, provider):
        with self.conn.atomic(join=True):
            provider_frontiers.clear(self.conn, provider)

    @synchronized
    def store_refresh_page(self, provider, refresh, stale, jobs, cursor):
        with self.conn.atomic(join=True):
            params = dict(ids=json.dumps(stale), provider=provider)
            if self.conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='provider_state_fields'").fetchone():
                self.conn.execute('DELETE FROM provider_state_fields WHERE provider=:provider AND recording_id IN '
                                  '(SELECT value FROM json_each(:ids))', params)
            self.conn.execute('DELETE FROM enrichment_provider_state WHERE provider=:provider '
                              'AND recording_id IN (SELECT value FROM json_each(:ids))', params)
            self.conn.executemany('INSERT OR IGNORE INTO enrichment_provider_jobs VALUES (?,?)', [(identity, provider) for identity in jobs])
            provider_frontiers.advance(self.conn, provider, refresh, cursor)

    @snapshot_read
    def provider_refresh_pending(self, provider):
        return provider_frontiers.read(self.conn,provider) is not None

    @snapshot_read
    def missing_provider_coverage(self, provider):
        if metadata_runtime.active(self.conn):
            return [row[0] for row in self.conn.execute('SELECT r.id FROM recordings r '
                'WHERE first_enriched_at IS NOT NULL AND EXISTS (SELECT 1 FROM enrichment_provider_state s WHERE s.recording_id=r.id) '
                'AND NOT EXISTS (SELECT 1 FROM enrichment_provider_state s WHERE s.recording_id=r.id '
                "AND s.provider=? AND s.status<>'retry') AND NOT EXISTS (SELECT 1 FROM enrichment_provider_jobs j "
                'WHERE j.recording_id=r.id AND j.provider=?) AND (EXISTS (SELECT 1 FROM catalog_entries e '
                'JOIN library_memberships l ON l.entry_id=e.id WHERE e.recording_id=r.id) OR EXISTS '
                '(SELECT 1 FROM discovery_memberships d WHERE d.recording_id=r.id))', (provider, provider))]
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
    def admit_provider_coverage(self, provider, current, candidates):
        with self.conn.atomic():
            if not self._provider_matches(provider, current):
                return False
            if metadata_runtime.active(self.conn):
                self.conn.execute('INSERT OR IGNORE INTO enrichment_provider_jobs SELECT t.recording_id,:provider '
                    'FROM json_each(:ids) selected JOIN catalog_enrichment_tracks t ON t.queue_order=selected.value '
                    'AND t.recording_id=t.queue_order WHERE t.first_enriched_at IS NOT NULL AND EXISTS '
                    '(SELECT 1 FROM enrichment_provider_state s WHERE s.recording_id=t.recording_id) AND NOT EXISTS '
                    "(SELECT 1 FROM enrichment_provider_state s WHERE s.recording_id=t.recording_id AND s.provider=:provider AND s.status<>'retry')",
                    dict(ids=json.dumps(candidates), provider=provider))
                return True
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
    def coverage_inputs(self, recording_id):
        return [dict(row) for row in self.conn.execute('SELECT c.provider,c.status configured,s.status,j.provider IS NOT NULL pending '
            'FROM enrichment_provider_config c LEFT JOIN enrichment_provider_state s ON s.provider=c.provider AND s.recording_id=? '
            'LEFT JOIN enrichment_provider_jobs j ON j.provider=c.provider AND j.recording_id=?', (recording_id, recording_id))]

    @synchronized
    def enrichment_attempts(self, identity_key, video_id=None):
        key = catalog.resolve_key(self.conn,identity_key,video_id)
        if metadata_runtime.active(self.conn):
            recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
            if recording is None:
                return {}
            return {name: state for name, state in self.enrichment_provider_state(recording[0], include_previous=True).items()
                    if video_id is None or metadata_runtime.alternatives(self.conn, recording[0], name).get('input_video_id') == video_id}
        if metadata_attempts.available(self.conn):
            rows = self.conn.execute('WITH relevant AS (SELECT p.*,ROW_NUMBER() OVER '
                '(PARTITION BY p.provider ORDER BY p.at DESC,p.id DESC) AS latest FROM provider_attempts p WHERE '
                '(p.recording_id=(SELECT id FROM recordings WHERE identity_key=?) OR p.input_video_id IN '
                '(SELECT y.video_id FROM youtube_items y JOIN recordings r ON r.id=y.recording_id WHERE r.identity_key=?)) '
                'AND (? IS NULL OR p.input_video_id=?)) SELECT * FROM relevant WHERE latest=1 ORDER BY at,id',
                (key, key, video_id, video_id)).fetchall()
            return {r['provider']: {'at': r['at'], 'status': r['status'],
                    'fields': metadata_attempts.attempt_fields(self.conn, r['id'])} for r in rows}
        rows = self.conn.execute(
            f"SELECT p.* FROM {metadata_publication.read_table(self.conn,'provider_attempts')} p WHERE "
            "(p.recording_id=(SELECT id FROM recordings WHERE identity_key=?) OR "
            "p.input_video_id IN (SELECT y.video_id FROM youtube_items y JOIN recordings r "
            "ON r.id=y.recording_id WHERE r.identity_key=?)) "
            "AND (? IS NULL OR p.input_video_id=?) ORDER BY p.at,p.id",
            (key,key,video_id,video_id)).fetchall()
        result = {}
        for row in rows:
            fields = metadata_publication.fields(self.conn,'provider_attempts',json.dumps([row['id']],separators=(',',':')))
            result[row['provider']] = {'at':row['at'],'status':row['status'],
                'fields':fields if fields is not None else json.loads(row['fields_json'])}
        return result

    @synchronized
    def store_enrichment_log(self, inputs, run_id, provider, field, value, at, *, scope, scheme, identifiers,
                             delivery_id, observation_id):
        entry, recording, video = inputs['entry_id'], inputs['recording_id'], inputs['video_id']
        with self.conn.atomic():
            current = self.conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()
            if current is None or tuple(current) != (recording, video):
                raise metadata_observations.SubjectChanged('Enrichment catalog subject changed during lookup')
            if inputs['attempts'] and identifiers:
                delivery = self.conn.execute('SELECT provider,input_entry_id,input_recording_id,input_video_id,observed_at '
                                            'FROM metadata_deliveries WHERE id=?', (delivery_id,)).fetchone()
                if delivery is None or tuple(delivery[:4]) != (provider, entry, recording, video):
                    raise ValueError('Identifier evidence has a different delivery or captured input')
                if field == 'mb_recording_id' and observation_id is None:
                    raise ValueError('A recording MBID requires its supplying catalog observation')
                for identifier in identifiers:
                    self.conn.execute('INSERT INTO recording_identifiers(recording_id,scheme,entity_type,value,source,input_video_id,status,'
                        "observed_at,delivery_id,observation_id) VALUES (?,?,'recording',?,?,?,'candidate',?,?,?) "
                        'ON CONFLICT(delivery_id,scheme,entity_type,value) WHERE delivery_id IS NOT NULL DO NOTHING',
                        (recording, scheme, identifier, provider, video, delivery['observed_at'], delivery_id, observation_id if field == 'mb_recording_id' else None))
            elif inputs['layout'] == 'runtime':
                for identifier in identifiers:
                    self.conn.execute("INSERT INTO recording_identifiers(recording_id,scheme,entity_type,value,source,input_video_id,status,observed_at) "
                        "SELECT ?,?,'recording',?,?,?,'candidate',? WHERE NOT EXISTS (SELECT 1 FROM recording_identifiers "
                        "WHERE recording_id=? AND scheme=? AND entity_type='recording' AND value=? AND source=? AND input_video_id IS ?)",
                        (recording, scheme, identifier, provider, video, at, recording, scheme, identifier, provider, video))
            elif not inputs['attempts']:
                self.conn.execute('INSERT INTO enrichment_log(track_id,run_id,provider,field,value,created_at,recording_id,input_video_id,field_scope) '
                    'VALUES (?,?,?,?,?,?,?,?,?)', (entry, run_id, provider, field, None if value is None else str(value), at, recording, video, scope))
                self.conn.executemany("INSERT INTO recording_identifiers(recording_id,scheme,entity_type,value,source,input_video_id,status,observed_at) "
                    "VALUES (?,?,'recording',?,?,?,'candidate',?)", [(recording, scheme, identifier, provider, video, at) for identifier in identifiers])
