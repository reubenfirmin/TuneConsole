"""SQL state for provider attempts, configuration, cooldowns and artwork retries."""
import json
from contextlib import contextmanager

from yt_playlist.core import catalog, metadata_observations, metadata_native, metadata_attempts, legacy_journals
from yt_playlist.core.legacy_evidence_contract import FORMAT as ORIGINAL_FORMAT
from yt_playlist.core import provider_frontiers
from yt_playlist.core import metadata_runtime, metadata_reads
from yt_playlist.core.preparation import metadata_destinations, metadata_publication
from yt_playlist.repos.base import Repo, snapshot_read, synchronized
from yt_playlist.core.db_writes import atomic_write

class EnrichmentStateRepo(Repo):
    @contextmanager
    def state_transaction(self):
        with atomic_write(self._store):
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
                'native': metadata_native.available(self.conn),
                'layout': 'runtime' if metadata_runtime.active(self.conn) else
                          'evidence' if metadata_attempts.available(self.conn) else 'legacy'}

    @synchronized
    def store_legacy_observations(self, inputs, rows):
        with self.conn.atomic():
            self._check_subject(inputs)
            self.conn.executemany('INSERT INTO metadata_findings(entry_id,recording_id,field,provider,value_json,'
                'observed_at,provenance_json,observation_key) VALUES (?,?,?,?,?,?,?,?) '
                'ON CONFLICT(observation_key) WHERE observation_key IS NOT NULL DO NOTHING', rows)

    def _check_subject(self, inputs):
        current = self.conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?',
                                    (inputs['entry_id'],)).fetchone()
        if current is None or tuple(current) != (inputs['recording_id'], inputs['video_id']):
            raise metadata_observations.SubjectChanged('Observation catalog subject changed during lookup')

    @synchronized
    def native_source_inputs(self, inputs, key):
        self._check_subject(inputs)
        source = self.conn.execute('SELECT revision FROM rec_musical_sources WHERE source_key=?', (key,)).fetchone()
        head = self.conn.execute('SELECT run_id,accepted_at,result_hash FROM metadata_native_delivery_heads WHERE source_key=?', (key,)).fetchone()
        legacy = None
        if head is None and metadata_native.available(self.conn):
            legacy = self.conn.execute('SELECT d.delivery_key,d.observed_at,d.raw_filename,d.raw_digest,d.raw_byte_size,d.raw_offset '
                'FROM musical_source_observations s JOIN native_metadata_observations o ON o.id=s.observation_id '
                'JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE s.source_key=? '
                'ORDER BY d.observed_at DESC,d.id DESC LIMIT 1', (key,)).fetchone()
            if legacy is not None and legacy['observed_at'] is None:
                legacy = None
        return {'source': dict(source) if source else None, 'head': dict(head) if head else None,
                'legacy': dict(legacy) if legacy else None}

    @synchronized
    def store_native_source(self, inputs, key, provider, scope, entity, update, run_id):
        with self.conn.atomic(join=True):
            self._check_subject(inputs)
            if update['revision'] is not None:
                self.conn.execute('INSERT INTO rec_musical_sources(source_key,provider,scope,entity_id,revision) VALUES (?,?,?,?,?) '
                    'ON CONFLICT(source_key) DO UPDATE SET revision=excluded.revision',
                    (key, provider, scope, entity, update['revision']))
            if update['head'] is not None:
                head = update['head']
                self.conn.execute('INSERT INTO metadata_native_delivery_heads VALUES (?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET '
                    'run_id=excluded.run_id,accepted_at=excluded.accepted_at,result_hash=excluded.result_hash',
                    (key, head['run_id'], head['accepted_at'], head['result_hash']))
            self._store_native_link(inputs['recording_id'], key, provider, scope, run_id)

    def _store_native_link(self, recording, key, provider, scope, run_id):
        self.conn.execute('DELETE FROM rec_musical_links WHERE recording_id=? AND source_key<>? AND delivery_id<>? '
            'AND EXISTS (SELECT 1 FROM rec_musical_sources s WHERE s.source_key=rec_musical_links.source_key '
            'AND s.provider=? AND s.scope=?)', (recording, key, run_id, provider, scope))
        self.conn.execute('INSERT INTO rec_musical_links(source_key,recording_id,delivery_id) VALUES (?,?,?) '
            'ON CONFLICT(source_key,recording_id) DO UPDATE SET delivery_id=excluded.delivery_id '
            'WHERE delivery_id<>excluded.delivery_id', (key, recording, run_id))

    @synchronized
    def native_observation_inputs(self, inputs, delivery, position, key, field):
        self._check_subject(inputs)
        existing = self.conn.execute('SELECT id FROM native_metadata_observations WHERE delivery_id=? AND position=?',
                                    (delivery, position)).fetchone()
        previous, source = None, None
        if key is not None:
            source = self.conn.execute('SELECT 1 FROM rec_musical_sources WHERE source_key=?', (key,)).fetchone()
            previous = self.conn.execute('SELECT o.*,d.raw_filename,d.raw_digest,d.raw_byte_size,d.raw_offset '
                'FROM musical_source_observations s JOIN native_metadata_observations o ON o.id=s.observation_id '
                'JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE s.source_key=? AND s.field=?', (key, field)).fetchone()
        return {'existing': existing is not None, 'source': source is not None, 'previous': dict(previous) if previous else None}

    @synchronized
    def store_native_observation(self, inputs, delivery, position, payload, key, provider, run_id, *, source_exists, changed):
        with self.conn.atomic(join=True):
            self._check_subject(inputs)
            identity = self.conn.execute('INSERT INTO native_metadata_observations(delivery_id,position,field,kind,unit) '
                'VALUES (?,?,?,?,?)', (delivery, position, payload['field'], payload['kind'], payload['unit'])).lastrowid
            if key is None:
                return
            scope, entity = payload['scope'], payload['entity_id']
            if not source_exists:
                has_payload = any(row[1] == 'payload' for row in self.conn.execute('PRAGMA table_info(rec_musical_sources)'))
                columns, placeholder = (',payload', ",'{}'") if has_payload else ('', '')
                self.conn.execute(f'INSERT INTO rec_musical_sources(source_key,provider,scope,entity_id{columns}) VALUES (?,?,?,?{placeholder})',
                                  (key, provider, scope, entity))
            elif changed:
                self.conn.execute('UPDATE rec_musical_sources SET revision=revision+1 WHERE source_key=?', (key,))
            self.conn.execute('INSERT INTO musical_source_observations(source_key,field,observation_id) VALUES (?,?,?) '
                'ON CONFLICT(source_key,field) DO UPDATE SET observation_id=excluded.observation_id '
                'WHERE observation_id<>excluded.observation_id', (key, payload['field'], identity))
            self._store_native_link(inputs['recording_id'], key, provider, scope, run_id)

    @synchronized
    def observation_inputs(self, identity_key, *, after_id, limit):
        """One recording's evidence in bounded, restartable ID order; legacy rows stay separate."""
        key = catalog.resolve_key(self.conn, identity_key)
        if metadata_runtime.active(self.conn):
            rows = self.conn.execute('SELECT s.rowid AS cursor,s.source_key,s.revision,s.provider '
                'FROM rec_musical_sources s JOIN rec_musical_links l ON l.source_key=s.source_key '
                'JOIN recordings r ON r.id=l.recording_id WHERE r.identity_key=? AND s.rowid>=? '
                'ORDER BY s.rowid LIMIT ?', (key, after_id // 64, limit))
            return {'layout': 'runtime', 'rows': [dict(row) for row in rows]}
        if metadata_native.available(self.conn):
            rows = self.conn.execute('SELECT o.*,d.provider,d.input_entry_id AS entry_id,'
                'd.input_recording_id AS recording_id,d.observed_at,d.delivery_key,'
                'd.raw_filename,d.raw_digest,d.raw_byte_size,d.raw_offset '
                'FROM native_metadata_observations o JOIN metadata_deliveries d ON d.id=o.delivery_id '
                'WHERE d.input_recording_id=(SELECT id FROM recordings WHERE identity_key=?) AND o.id>? '
                'ORDER BY o.id LIMIT ?', (key, after_id, limit)).fetchall()
            return {'layout': 'native', 'rows': [{**dict(row), 'reference': tuple(row[name] for name in
                ('raw_filename', 'raw_digest', 'raw_byte_size', 'raw_offset'))} for row in rows]}
        recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?',(key,)).fetchone()
        rows = metadata_destinations.native_references(self.conn,recording=recording[0] if recording else None,after=after_id,limit=limit)
        if rows is not None:
            return {'layout': 'prepared', 'rows': rows}
        rows = self.conn.execute(
            'SELECT f.* FROM '+metadata_publication.read_table(self.conn,'metadata_findings')+' f WHERE f.recording_id='
            '(SELECT id FROM recordings WHERE identity_key=?) AND f.id>? '
            'AND f.provenance_json IS NOT NULL ORDER BY f.id LIMIT ?', (key, after_id, limit))
        return {'layout': 'legacy', 'rows': [{**dict(row), 'value': json.loads(row['value_json']),
                 'provenance': json.loads(row['provenance_json'])} for row in rows]}

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
    def attempt_context(self, identity_key, provider, video_id=None, *, entry_id=None, delivery_id=None):
        key = catalog.resolve_key(self.conn, identity_key, video_id)
        row = self.conn.execute("SELECT id,COALESCE(last_enriched_at,-1) cycle FROM recordings WHERE identity_key=?", (key,)).fetchone()
        layout = ('runtime' if metadata_runtime.active(self.conn) else
                  'evidence' if metadata_attempts.available(self.conn) else 'legacy')
        if row is None:
            return {'layout': layout, 'subject': None}
        entry = self.conn.execute('SELECT id AS entry_id,recording_id,video_id FROM catalog_entries WHERE recording_id=? '
            'AND (? IS NULL OR video_id=?) AND (? IS NULL OR id=?) ORDER BY id LIMIT 1',
            (row['id'], video_id, video_id, entry_id, entry_id)).fetchone()
        previous = self.conn.execute('SELECT * FROM enrichment_provider_state WHERE recording_id=? AND provider=?',
                                     (row['id'], provider)).fetchone()
        attempt = self.conn.execute('SELECT id,status FROM provider_attempts WHERE delivery_id=?',
                                    (delivery_id,)).fetchone() if layout == 'evidence' and delivery_id is not None else None
        provider_id = metadata_runtime.provider_id(self.conn, provider, create=False) if layout == 'runtime' else None
        return {'layout': layout, 'subject': dict(entry) if entry else None, 'recording_id': row['id'],
                'cycle': row['cycle'], 'previous': dict(previous) if previous else None,
                'provider_id': provider_id, 'attempt': dict(attempt) if attempt else None}

    @synchronized
    def unresolved_attempt_inputs(self, source_id):
        journal = legacy_journals.available(self.conn)
        receipts = self.conn.execute("SELECT 1 FROM sqlite_schema WHERE name='metadata_legacy_evidence'").fetchone() is not None
        reference = self.conn.execute('SELECT filename,content_digest,byte_size,file_offset FROM metadata_legacy_evidence '
            "WHERE kind='enrichment_unresolved_attempt' AND source_id=?", (source_id,)).fetchone() if receipts else None
        return {'source_id': source_id, 'layout': 'journal' if journal else 'receipt' if receipts else 'inline',
                'publication': legacy_journals.head(self.conn, 'metadata') if journal else None,
                'reference': tuple(reference) if reference else None}

    @synchronized
    def store_unresolved_attempt(self, inputs, identity_key, payload, *, publication=None, reference=None):
        with self.conn.atomic(join=True):
            if inputs['layout'] == 'inline':
                self.conn.execute("INSERT OR REPLACE INTO legacy_enrichment_attempts VALUES (?,?,?)",
                                  (identity_key, payload, 'unresolved input identity'))
            elif inputs['layout'] == 'journal':
                self.conn.execute('INSERT INTO legacy_archive_heads VALUES (?,?) ON CONFLICT(domain) '
                    'DO UPDATE SET publication=excluded.publication', ('metadata', bytes.fromhex(publication)))
            else:
                self.conn.execute('INSERT INTO metadata_legacy_evidence(kind,source_id,reason,filename,content_digest,'
                    'byte_size,file_offset,format_version,resolution_status) VALUES (?,?,?,?,?,?,?,?,?)',
                    ('enrichment_unresolved_attempt', inputs['source_id'], 'unresolved input identity',
                     *reference, ORIGINAL_FORMAT, 'unresolved'))

    @synchronized
    def reserve_provider_identity(self, provider):
        return metadata_runtime.provider_id(self.conn, provider)

    @synchronized
    def store_attempt(self, inputs, provider, fields, result, *, delivery_id=None, observations=None, video_id=None):
        subject, layout = inputs['subject'], inputs['layout']
        recording, cycle = inputs['recording_id'], inputs['cycle']
        with self.conn.atomic(join=True):
            self._check_subject(subject)
            if layout == 'legacy':
                self.conn.execute('INSERT INTO provider_attempts(recording_id,input_video_id,provider,at,status,fields_json) '
                    'VALUES (?,?,?,?,?,?)', (recording, video_id or '', provider, result['at'], result['status'], json.dumps(fields)))
                self.conn.execute('INSERT OR REPLACE INTO enrichment_provider_state VALUES (?,?,?,?,?,?,?,?,?)',
                    (recording, provider, cycle, result['at'], result['status'], json.dumps(result['fields']),
                     result['failures'], result['retry_at'], result['error']))
                return
            if layout == 'evidence':
                has_json = any(row[1] == 'fields_json' for row in self.conn.execute('PRAGMA table_info(provider_attempts)'))
                columns, placeholder = (',fields_json', ",'{}'") if has_json else ('', '')
                identity = self.conn.execute('INSERT INTO provider_attempts(recording_id,input_video_id,provider,at,status,delivery_id'
                    + columns + ') VALUES (?,?,?,?,?,?' + placeholder + ')',
                    (recording, subject['video_id'] or '', provider, result['at'], result['status'], delivery_id)).lastrowid
                self.conn.executemany('INSERT INTO provider_attempt_fields(attempt_id,field,observation_id) VALUES (?,?,?)',
                    [(identity, field, observations.get('mb_year' if field == 'year' else field)) for field in fields])
                if result['reset_fields']:
                    self.conn.execute('DELETE FROM provider_state_fields WHERE recording_id=? AND provider=?', (recording, provider))
            has_json = any(row[1] == 'fields_json' for row in self.conn.execute('PRAGMA table_info(enrichment_provider_state)'))
            columns, placeholder = (',fields_json', ",'{}'") if has_json else ('', '')
            self.conn.execute('INSERT INTO enrichment_provider_state(recording_id,provider,cycle,at,status,failures,retry_at,error'
                + columns + ') VALUES (?,?,?,?,?,?,?,?' + placeholder + ') ON CONFLICT(recording_id,provider) DO UPDATE SET '
                'cycle=excluded.cycle,at=excluded.at,status=excluded.status,failures=excluded.failures,'
                'retry_at=excluded.retry_at,error=excluded.error',
                (recording, provider, cycle, result['at'], result['status'], result['failures'], result['retry_at'], result['error']))
            if layout == 'evidence':
                self.conn.executemany('INSERT INTO provider_state_fields(recording_id,provider,field,delivery_id,observation_id) '
                    'VALUES (?,?,?,?,?) ON CONFLICT(recording_id,provider,field) DO UPDATE SET '
                    'delivery_id=excluded.delivery_id,observation_id=excluded.observation_id',
                    [(recording, provider, field, delivery_id, observations.get('mb_year' if field == 'year' else field)) for field in fields])

    @synchronized
    def completion_inputs(self, recording_id, provider):
        jobs = [row[0] for row in self.conn.execute('SELECT provider FROM enrichment_provider_jobs WHERE recording_id=?', (recording_id,))]
        if provider not in jobs:
            return None
        if metadata_runtime.active(self.conn):
            rows = self.conn.execute('SELECT s.*,p.id provider_id FROM enrichment_provider_state s '
                'LEFT JOIN metadata_runtime_providers p ON p.name=s.provider '
                "WHERE s.recording_id=? AND s.status<>'retry' AND NOT EXISTS(SELECT 1 FROM enrichment_provider_jobs j "
                'WHERE j.recording_id=s.recording_id AND j.provider=s.provider)', (recording_id,))
            states = []
            for row in rows:
                state = dict(row)
                states.append({'provider_id': state.pop('provider_id'), 'state': state})
            return {'states': {'runtime': True, 'subject': None, 'rows': states}}
        if metadata_attempts.available(self.conn):
            return {'answers': metadata_attempts.completed_genres(self.conn, recording_id)}
        rows = self.conn.execute("SELECT s.provider,s.fields_json FROM enrichment_provider_state s WHERE s.recording_id=? "
            "AND s.status<>'retry' AND NOT EXISTS(SELECT 1 FROM enrichment_provider_jobs j "
            'WHERE j.recording_id=s.recording_id AND j.provider=s.provider)', (recording_id,))
        return {'answers': {row['provider']: json.loads(row['fields_json']).get('genre') for row in rows}}

    @synchronized
    def finish_provider_job(self, recording_id, provider):
        with self.conn.atomic():
            self.conn.execute('DELETE FROM enrichment_provider_jobs WHERE recording_id=? AND provider=?', (recording_id, provider))

    @synchronized
    def provider_state_inputs(self, recording_id, *, include_previous=False, entry_id=None):
        if metadata_runtime.active(self.conn):
            rows = [dict(row) for row in self.conn.execute('SELECT s.*,p.id provider_id '
                'FROM enrichment_provider_state s JOIN recordings r ON r.id=s.recording_id '
                'LEFT JOIN metadata_runtime_providers p ON p.name=s.provider '
                'WHERE s.recording_id=? AND (? OR s.cycle=COALESCE(r.last_enriched_at,-1))',
                (recording_id, include_previous))]
            subject = None
            if entry_id is not None and rows:
                recording, video = metadata_runtime.subject(self.conn, entry_id)
                subject = {'entry_id': entry_id, 'recording_id': recording, 'video_id': video}
            return {'runtime': True, 'subject': subject,
                    'rows': [{'provider_id': row.pop('provider_id'), 'state': row} for row in rows]}

        rows = self.conn.execute(
            f"SELECT s.* FROM {metadata_publication.read_table(self.conn,'enrichment_provider_state')} s JOIN recordings r ON r.id=s.recording_id "
            "WHERE s.recording_id=? AND (? OR s.cycle=COALESCE(r.last_enriched_at,-1))",
            (recording_id, include_previous)).fetchall()
        typed = metadata_attempts.available(self.conn)
        result = []
        for row in rows:
            if typed:
                references = self.conn.execute('SELECT f.field,f.delivery_id,d.raw_filename,d.raw_digest,d.raw_byte_size,d.raw_offset '
                    'FROM provider_state_fields f JOIN metadata_deliveries d ON d.id=f.delivery_id '
                    'WHERE f.recording_id=? AND f.provider=? ORDER BY f.field', (recording_id, row['provider']))
                fields = {'deliveries': [self._delivery_field(reference) for reference in references]}
            else:
                fields = self._field_input('enrichment_provider_state', [recording_id, row['provider']], row['fields_json'])
            result.append({'state': dict(row), 'fields': fields})
        return {'runtime': False, 'rows': result}

    @staticmethod
    def _delivery_field(row):
        return {'field': row['field'], 'delivery_id': row['delivery_id'],
                'reference': tuple(row[name] for name in ('raw_filename', 'raw_digest', 'raw_byte_size', 'raw_offset'))}

    def _field_input(self, owner, key, raw):
        key = json.dumps(key, separators=(',', ':'))
        reference = metadata_reads.operational_reference(self.conn, owner, key)
        if reference is not None:
            return {'prepared': reference}
        revision = metadata_publication.selected(self.conn)
        if revision is not None and self.conn.execute('SELECT 1 FROM '+metadata_publication.table('evidence')+
                ' WHERE stage=? AND owner=? AND source_key=?', (revision, owner, key)).fetchone():
            return {'values': {row['field']: json.loads(row['raw_value']) for row in self.conn.execute(
                'SELECT field,raw_value FROM '+metadata_publication.table('operational_fields')+
                ' WHERE stage=? AND owner=? AND source_key=? ORDER BY position', (revision, owner, key))}}
        return {'values': json.loads(raw)}

    @synchronized
    def expiry_state(self):
        if not metadata_runtime.active(self.conn):
            return None
        return dict(self.conn.execute('SELECT gc_cursor,gc_after,gc_shard FROM metadata_runtime_state WHERE id=1').fetchone())

    @synchronized
    def expiry_page(self, cursor, *, limit):
        rows = []
        for (recording,) in self.conn.execute('SELECT id FROM recordings WHERE id>? ORDER BY id LIMIT ?', (cursor, limit)).fetchall():
            conflicts = [dict(row) for row in self.conn.execute('SELECT * FROM metadata_runtime_conflicts '
                'WHERE recording_id=? OR entry_id IN (SELECT id FROM catalog_entries WHERE recording_id=?)', (recording, recording))]
            sources = [dict(row) for row in self.conn.execute('SELECT s.source_key,s.revision FROM rec_musical_sources s '
                'JOIN rec_musical_links l ON l.source_key=s.source_key WHERE l.recording_id=?', (recording,))]
            rows.append({'recording_id': recording, 'conflicts': conflicts, 'sources': sources})
        return rows

    @synchronized
    def store_expiry_shard(self, shard):
        self.conn.execute('UPDATE metadata_runtime_state SET gc_shard=? WHERE id=1', (shard,))

    @synchronized
    def store_conflict_revision(self, identity, revision):
        self.conn.execute('UPDATE metadata_runtime_conflicts SET revision=? WHERE id=?', (revision, identity))

    @synchronized
    def store_expiry_cursor(self, cursor, after):
        self.conn.execute('UPDATE metadata_runtime_state SET gc_cursor=?,gc_after=? WHERE id=1', (cursor, after))

    @synchronized
    def enrichment_cooldowns(self):
        return {r["provider"]: dict(r) for r in self.conn.execute("SELECT * FROM enrichment_provider_cooldowns")}

    @synchronized
    def retry_status_inputs(self, provider, ready_sql):
        row = self.conn.execute(
            "SELECT COUNT(*) OVER () AS count,s.error,MAX(s.retry_at,COALESCE(c.retry_at,0)) retry_at "
            f"FROM {metadata_publication.read_table(self.conn,'enrichment_provider_state')} s JOIN recordings r ON r.id=s.recording_id "
            "LEFT JOIN enrichment_provider_cooldowns c ON c.provider=s.provider "
            "LEFT JOIN enrichment_provider_config cfg ON cfg.provider=s.provider "
            "WHERE s.status='retry' AND s.cycle=COALESCE(r.last_enriched_at,-1) "
            "AND (cfg.provider IS NULL OR cfg.status='available') "
            "AND (s.provider<>? OR EXISTS (SELECT 1 FROM catalog_enrichment_tracks t "
            "WHERE t.recording_id=r.id AND " + ready_sql + ")) "
            "ORDER BY retry_at,s.at LIMIT 1", (provider,)).fetchone()
        return dict(row) if row else None

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
    def missing_provider_coverage(self, provider, *, after_id, limit):
        if metadata_runtime.active(self.conn):
            return [row[0] for row in self.conn.execute('SELECT r.id FROM recordings r '
                'WHERE r.id>? AND first_enriched_at IS NOT NULL AND EXISTS (SELECT 1 FROM enrichment_provider_state s WHERE s.recording_id=r.id) '
                'AND NOT EXISTS (SELECT 1 FROM enrichment_provider_state s WHERE s.recording_id=r.id '
                "AND s.provider=? AND s.status<>'retry') AND NOT EXISTS (SELECT 1 FROM enrichment_provider_jobs j "
                'WHERE j.recording_id=r.id AND j.provider=?) AND (EXISTS (SELECT 1 FROM catalog_entries e '
                'JOIN library_memberships l ON l.entry_id=e.id WHERE e.recording_id=r.id) OR EXISTS '
                '(SELECT 1 FROM discovery_memberships d WHERE d.recording_id=r.id)) ORDER BY r.id LIMIT ?',
                (after_id, provider, provider, limit))]
        return [row[0] for row in self.conn.execute(
            'SELECT r.id FROM recordings r WHERE r.id>? AND first_enriched_at IS NOT NULL '
            'AND EXISTS (SELECT 1 FROM provider_attempts p WHERE p.recording_id=r.id) '
            'AND NOT EXISTS (SELECT 1 FROM provider_attempts p WHERE p.recording_id=r.id '
            "AND p.provider=? AND p.status<>'retry') AND NOT EXISTS (SELECT 1 FROM enrichment_provider_jobs j "
            'WHERE j.recording_id=r.id AND j.provider=?) AND ('
            'EXISTS (SELECT 1 FROM catalog_entries e JOIN library_memberships l ON l.entry_id=e.id '
            'WHERE e.recording_id=r.id) OR EXISTS (SELECT 1 FROM discovery_memberships d '
            'WHERE d.recording_id=r.id)) ORDER BY r.id LIMIT ?', (after_id, provider, provider, limit))]

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
    def attempt_inputs(self, identity_key, video_id=None):
        key = catalog.resolve_key(self.conn,identity_key,video_id)
        if metadata_runtime.active(self.conn):
            recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
            if recording is None:
                return {'runtime': True, 'subject': None, 'rows': []}
            return self.provider_state_inputs(recording[0], include_previous=True)
        if metadata_attempts.available(self.conn):
            rows = self.conn.execute('WITH relevant AS (SELECT p.*,ROW_NUMBER() OVER '
                '(PARTITION BY p.provider ORDER BY p.at DESC,p.id DESC) AS latest FROM provider_attempts p WHERE '
                '(p.recording_id=(SELECT id FROM recordings WHERE identity_key=?) OR p.input_video_id IN '
                '(SELECT y.video_id FROM youtube_items y JOIN recordings r ON r.id=y.recording_id WHERE r.identity_key=?)) '
                'AND (? IS NULL OR p.input_video_id=?)) SELECT * FROM relevant WHERE latest=1 ORDER BY at,id',
                (key, key, video_id, video_id)).fetchall()
            result = []
            for row in rows:
                references = self.conn.execute('SELECT f.field,a.delivery_id,d.raw_filename,d.raw_digest,d.raw_byte_size,d.raw_offset '
                    'FROM provider_attempt_fields f JOIN provider_attempts a ON a.id=f.attempt_id '
                    'JOIN metadata_deliveries d ON d.id=a.delivery_id WHERE f.attempt_id=? ORDER BY f.field', (row['id'],))
                result.append({'provider': row['provider'], 'at': row['at'], 'status': row['status'],
                               'fields': {'deliveries': [self._delivery_field(reference) for reference in references]}})
            return {'runtime': False, 'rows': result}
        rows = self.conn.execute(
            f"SELECT p.* FROM {metadata_publication.read_table(self.conn,'provider_attempts')} p WHERE "
            "(p.recording_id=(SELECT id FROM recordings WHERE identity_key=?) OR "
            "p.input_video_id IN (SELECT y.video_id FROM youtube_items y JOIN recordings r "
            "ON r.id=y.recording_id WHERE r.identity_key=?)) "
            "AND (? IS NULL OR p.input_video_id=?) ORDER BY p.at,p.id",
            (key,key,video_id,video_id)).fetchall()
        return {'runtime': False, 'rows': [{'provider': row['provider'], 'at': row['at'], 'status': row['status'],
            'fields': self._field_input('provider_attempts', [row['id']], row['fields_json'])} for row in rows]}

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
