"""SQL state for provider attempts, configuration, cooldowns and artwork retries."""
import json
from contextlib import contextmanager

from yt_playlist.core import catalog, metadata_observations, metadata_attempts, metadata_runtime
from yt_playlist.core import metadata_native, provider_credit_fields, provider_frontiers
from yt_playlist.repos.base import Repo, lookup_read, snapshot_read, synchronized
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.core.catalog_query import enrichment_recordings_sql, enrichment_tracks_sql
from yt_playlist.core.metadata_contract import EXPIRY
from yt_playlist.repos import metadata_file_writes
from yt_playlist.repos.enrichment_sql import QUEUE_PROVIDER, QUEUED_VALUES, SET_QUEUED


_ENRICHMENT_SUBJECT = enrichment_recordings_sql('SELECT r.id,r.identity_key')
_ENRICHMENT_ENTRY = enrichment_recordings_sql(
    'SELECT r.id,r.identity_key FROM recordings r WHERE r.id=e.recording_id'
)
_ENRICHMENT_SELECTED = enrichment_tracks_sql(
    'SELECT id,identity_key FROM recordings WHERE id IN (SELECT value FROM json_each(:ids))'
)
_RESULT_COLUMNS = 's.recording_id,s.provider,s.cycle,s.at,s.status,s.failures,s.retry_at,s.error'


class EnrichmentStateRepo(Repo):
    @synchronized
    def reserve_file(self, record):
        if self.conn.in_transaction:
            raise ValueError('Metadata file reservation requires its own transaction')
        with self.conn.atomic():
            return metadata_file_writes.reserve(self.conn, record)

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
            subject = self.conn.execute('SELECT recording_id FROM track_sources WHERE id=?', (entry_id,)).fetchone()
            if subject is None or subject[0] != recording_id:
                raise metadata_observations.SubjectChanged('Enrichment catalog subject changed during lookup')
            yield

    @synchronized
    def capture_inputs(self, entry_id):
        row = self.conn.execute('SELECT recording_id,video_id FROM track_sources WHERE id=?', (entry_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown provider result input')
        runtime = metadata_runtime.active(self.conn)
        return {'entry_id': entry_id, **dict(row), 'attempts': not runtime,
                'layout': 'runtime' if runtime else 'evidence'}

    def _check_subject(self, inputs):
        current = self.conn.execute('SELECT recording_id,video_id FROM track_sources WHERE id=?',
                                    (inputs['entry_id'],)).fetchone()
        if current is None or tuple(current) != (inputs['recording_id'], inputs['video_id']):
            raise metadata_observations.SubjectChanged('Observation catalog subject changed during lookup')

    @synchronized
    def native_source_inputs(self, inputs, key):
        self._check_subject(inputs)
        source = self.conn.execute('SELECT revision FROM rec_musical_sources WHERE source_key=?', (key,)).fetchone()
        head = metadata_native.runtime_head(self.conn, key)
        legacy = None
        if head is None:
            legacy = self.conn.execute('SELECT d.delivery_key,d.observed_at,l.raw_filename,l.raw_digest,l.raw_byte_size,l.raw_offset '
                + metadata_native.SELECTED_OBSERVATIONS +
                'JOIN metadata_deliveries d ON d.id=o.delivery_id JOIN metadata_delivery_locations l ON l.id=d.id '
                'WHERE s.source_key=? '
                'ORDER BY d.observed_at DESC,d.id DESC LIMIT 1', (key,)).fetchone()
            if legacy is not None and legacy['observed_at'] is None:
                legacy = None
        return {'source': dict(source) if source else None, 'head': dict(head) if head else None,
                'legacy': dict(legacy) if legacy else None,
                'file': metadata_file_writes.reference(self.conn, 'native', (key,), source['revision']) if source else None}

    @synchronized
    def store_native_source(self, inputs, key, provider, scope, entity, update, run_id, *, file_record=None):
        with self.conn.atomic(join=True):
            self._check_subject(inputs)
            if update['revision'] is not None:
                if file_record is None:
                    raise ValueError('Native metadata source requires its reserved file')
                metadata_file_writes.complete(self.conn, file_record, kind='native', owner=(key,), revision=update['revision'])
                self.conn.execute('INSERT INTO rec_musical_sources(source_key,provider,scope,entity_id,revision) VALUES (?,?,?,?,?) '
                    'ON CONFLICT(source_key) DO UPDATE SET revision=excluded.revision',
                    (key, provider, scope, entity, update['revision']))
            if update['head'] is not None:
                head = update['head']
                metadata_native.retain_runtime_head(self.conn, key, head)
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
            previous = self.conn.execute('SELECT o.*,l.raw_filename,l.raw_digest,l.raw_byte_size,l.raw_offset '
                'FROM rec_musical_sources s JOIN native_metadata_observations o ON o.id=s.selected_observations->>? '
                'JOIN metadata_delivery_locations l ON l.id=o.delivery_id WHERE s.source_key=?', (field, key)).fetchone()
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
                self.conn.execute('INSERT INTO rec_musical_sources(source_key,provider,scope,entity_id) VALUES (?,?,?,?)',
                                  (key, provider, scope, entity))
            elif changed:
                self.conn.execute('UPDATE rec_musical_sources SET revision=revision+1 WHERE source_key=?', (key,))
            metadata_native.select_observation(self.conn, key, payload['field'], identity)
            self._store_native_link(inputs['recording_id'], key, provider, scope, run_id)

    @synchronized
    def observation_inputs(self, identity_key, *, after_id, limit):
        """One recording's evidence in bounded, restartable ID order."""
        key = catalog.resolve_key(self.conn, identity_key)
        if metadata_runtime.active(self.conn):
            rows = self.conn.execute('SELECT s.rowid AS cursor,s.source_key,s.revision,s.provider '
                'FROM rec_musical_sources s JOIN rec_musical_links l ON l.source_key=s.source_key '
                'JOIN recordings r ON r.id=l.recording_id WHERE r.identity_key=? AND s.rowid>=? '
                'ORDER BY s.rowid LIMIT ?', (key, after_id // 64, limit))
            return {'layout': 'runtime', 'rows': [dict(row) | {'file': metadata_file_writes.reference(
                self.conn, 'native', (row['source_key'],), row['revision'])} for row in rows]}
        rows = self.conn.execute('SELECT o.*,d.provider,d.input_entry_id AS entry_id,'
            'd.input_recording_id AS recording_id,d.observed_at,d.delivery_key,'
            'l.raw_filename,l.raw_digest,l.raw_byte_size,l.raw_offset '
            'FROM native_metadata_observations o JOIN metadata_deliveries d ON d.id=o.delivery_id '
            'JOIN metadata_delivery_locations l ON l.id=d.id '
            'WHERE d.input_recording_id=(SELECT id FROM recordings WHERE identity_key=?) AND o.id>? '
            'ORDER BY o.id LIMIT ?', (key, after_id, limit))
        return {'layout': 'native', 'rows': [{**dict(row), 'reference': tuple(row[name] for name in
            ('raw_filename', 'raw_digest', 'raw_byte_size', 'raw_offset'))} for row in rows]}

    @synchronized
    def integration_cooldown(self, provider):
        row = self.conn.execute('SELECT provider,cooldown_failures failures,cooldown_retry_at retry_at,cooldown_error error '
                                'FROM enrichment_provider_config WHERE provider=? AND cooldown_failures IS NOT NULL',
                                (provider,)).fetchone()
        return dict(row) if row else None

    @synchronized
    def store_integration_cooldown(self, result):
        with self.conn.atomic(join=True):
            self.conn.execute('INSERT INTO enrichment_provider_config(provider,cooldown_failures,cooldown_retry_at,cooldown_error) '
                              'VALUES (?,?,?,?) ON CONFLICT(provider) DO UPDATE SET cooldown_failures=excluded.cooldown_failures,'
                              'cooldown_retry_at=excluded.cooldown_retry_at,cooldown_error=excluded.cooldown_error',
                              tuple(result[key] for key in ('provider', 'failures', 'retry_at', 'error')))

    @synchronized
    def clear_integration_cooldown(self, provider, admitted_deadline):
        """A completed request cannot erase a newer outage from another process."""
        with self.conn:
            self.conn.execute('UPDATE enrichment_provider_config SET cooldown_failures=NULL,cooldown_retry_at=NULL,cooldown_error=NULL '
                              'WHERE provider=? AND cooldown_retry_at<=?',
                              (provider, admitted_deadline))

    @lookup_read
    def release_check(self):
        row = self.conn.execute('SELECT release_checked_at,release_check_after,latest_release '
                                "FROM enrichment_provider_config WHERE provider='github'").fetchone()
        return tuple(row) if row else (None, None, None)

    @synchronized
    def schedule_release_check(self, deadline):
        with self.conn.atomic():
            self.conn.execute("INSERT INTO enrichment_provider_config(provider,release_check_after) VALUES ('github',?) "
                'ON CONFLICT(provider) DO UPDATE SET release_check_after=excluded.release_check_after '
                'WHERE release_checked_at IS NULL AND release_check_after IS NULL AND latest_release IS NULL', (deadline,))

    @synchronized
    def record_release_check(self, now, latest):
        with self.conn.atomic():
            self.conn.execute('INSERT INTO enrichment_provider_config(provider,release_checked_at,latest_release) '
                "VALUES ('github',?,?) ON CONFLICT(provider) DO UPDATE SET release_checked_at=excluded.release_checked_at,"
                'latest_release=coalesce(excluded.latest_release,enrichment_provider_config.latest_release) '
                'WHERE release_checked_at IS NULL OR release_checked_at<=excluded.release_checked_at', (now, latest))

    @synchronized
    def attempt_context(self, identity_key, provider, video_id=None, *, entry_id=None, delivery_id=None):
        key = catalog.resolve_key(self.conn, identity_key, video_id)
        row = self.conn.execute("SELECT id,COALESCE(last_enriched_at,-1) cycle FROM recordings WHERE identity_key=?", (key,)).fetchone()
        layout = 'runtime' if metadata_runtime.active(self.conn) else 'evidence'
        if row is None:
            return {'layout': layout, 'subject': None}
        entry = self.conn.execute('SELECT id AS entry_id,recording_id,video_id FROM track_sources WHERE recording_id=? '
            'AND (? IS NULL OR video_id=?) AND (? IS NULL OR id=?) ORDER BY id LIMIT 1',
            (row['id'], video_id, video_id, entry_id, entry_id)).fetchone()
        previous = self.conn.execute(f"SELECT {_RESULT_COLUMNS} FROM enrichment_provider_state s "
                                     "WHERE recording_id=? AND provider=? AND status<>'queued'",
                                     (row['id'], provider)).fetchone()
        attempt = self.conn.execute('SELECT id,status FROM provider_attempts WHERE delivery_id=?',
                                    (delivery_id,)).fetchone() if layout == 'evidence' and delivery_id is not None else None
        provider_id = metadata_runtime.provider_id(self.conn, provider, create=False) if layout == 'runtime' else None
        return {'layout': layout, 'subject': dict(entry) if entry else None, 'recording_id': row['id'],
                'cycle': row['cycle'], 'previous': dict(previous) if previous else None,
                'provider_id': provider_id, 'attempt': dict(attempt) if attempt else None,
                'file': metadata_file_writes.reference(self.conn, 'alternatives', (row['id'], provider_id),
                    float(previous['at'])) if layout == 'runtime' and previous and provider_id else None}

    @synchronized
    def reserve_provider_identity(self, provider):
        if self.conn.in_transaction:
            raise ValueError('Metadata provider reservation requires its own transaction')
        with self.conn.atomic():
            return metadata_runtime.provider_id(self.conn, provider)

    @synchronized
    def store_attempt(self, inputs, provider, fields, result, *, delivery_id=None, observations=None,
                      file_record=None):
        subject, layout = inputs['subject'], inputs['layout']
        recording, cycle = inputs['recording_id'], inputs['cycle']
        with self.conn.atomic(join=True):
            self._check_subject(subject)
            if layout == 'runtime':
                if file_record is None:
                    raise ValueError('Provider attempt requires its reserved file')
                metadata_file_writes.complete(self.conn, file_record, kind='alternatives',
                    owner=(recording, inputs['provider_id']), revision=result['at'])
            if layout == 'evidence':
                _, links = metadata_attempts.save_attempt(self.conn, recording, subject['video_id'], provider,
                    result['at'], result['status'], delivery_id, fields, observations)
                metadata_attempts.save_state(self.conn, recording, provider, cycle, result,
                    delivery=delivery_id, links=links, artist_credits='artist_credits' in fields)
                return
            self.conn.execute('INSERT INTO enrichment_provider_state(recording_id,provider,cycle,at,status,failures,retry_at,error) '
                'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(recording_id,provider) DO UPDATE SET '
                'cycle=excluded.cycle,at=excluded.at,status=excluded.status,failures=excluded.failures,'
                'retry_at=excluded.retry_at,error=excluded.error',
                (recording, provider, cycle, result['at'], result['status'], result['failures'], result['retry_at'], result['error']))

    @synchronized
    def completion_inputs(self, recording_id, provider):
        if not self.conn.execute('SELECT 1 FROM enrichment_provider_state WHERE recording_id=? AND provider=? AND queued=1',
                                 (recording_id, provider)).fetchone():
            return None
        if metadata_runtime.active(self.conn):
            rows = self.conn.execute(f'SELECT {_RESULT_COLUMNS},p.metadata_provider_id provider_id FROM enrichment_provider_state s '
                'LEFT JOIN enrichment_provider_config p ON p.provider=s.provider '
                "WHERE s.recording_id=? AND s.status<>'retry' AND s.queued=0", (recording_id,))
            states = []
            for row in rows:
                state = dict(row)
                provider_id = state.pop('provider_id')
                states.append({'provider_id': provider_id, 'state': state,
                    'file': metadata_file_writes.reference(self.conn, 'alternatives',
                        (recording_id, provider_id), float(state['at'])) if provider_id else None})
            return {'states': {'runtime': True, 'subject': None, 'rows': states}}
        return {'answers': metadata_attempts.completed_genres(self.conn, recording_id)}

    @synchronized
    def finish_provider_job(self, recording_id, provider):
        with self.conn.atomic():
            args = (recording_id, provider)
            self.conn.execute("DELETE FROM enrichment_provider_state WHERE recording_id=? AND provider=? AND status='queued'", args)
            self.conn.execute('UPDATE enrichment_provider_state SET queued=0 WHERE recording_id=? AND provider=? AND queued=1', args)

    @synchronized
    def provider_state_inputs(self, recording_id, *, include_previous=False, entry_id=None):
        if metadata_runtime.active(self.conn):
            rows = [dict(row) for row in self.conn.execute(f'SELECT {_RESULT_COLUMNS},p.metadata_provider_id provider_id '
                'FROM enrichment_provider_state s JOIN recordings r ON r.id=s.recording_id '
                'LEFT JOIN enrichment_provider_config p ON p.provider=s.provider '
                "WHERE s.recording_id=? AND s.status<>'queued' AND (? OR s.cycle=COALESCE(r.last_enriched_at,-1))",
                (recording_id, include_previous))]
            subject = None
            if entry_id is not None and rows:
                recording, video = metadata_runtime.subject(self.conn, entry_id)
                subject = {'entry_id': entry_id, 'recording_id': recording, 'video_id': video}
            result = []
            for row in rows:
                provider_id = row.pop('provider_id')
                result.append({'provider_id': provider_id, 'state': row,
                    'file': metadata_file_writes.reference(self.conn, 'alternatives',
                        (recording_id, provider_id), float(row['at'])) if provider_id else None})
            return {'runtime': True, 'subject': subject, 'rows': result}

        rows = self.conn.execute(
            f"SELECT {_RESULT_COLUMNS} FROM enrichment_provider_state s JOIN recordings r ON r.id=s.recording_id "
            "WHERE s.recording_id=? AND s.status<>'queued' AND (? OR s.cycle=COALESCE(r.last_enriched_at,-1))",
            (recording_id, include_previous)).fetchall()
        result = []
        for row in rows:
            references = self.conn.execute('SELECT f.field,f.delivery_id,l.raw_filename,l.raw_digest,l.raw_byte_size,l.raw_offset '
                'FROM ' + provider_credit_fields.STATE_SOURCE + ' f JOIN metadata_delivery_locations l ON l.id=f.delivery_id '
                'WHERE f.recording_id=? AND f.provider=? ORDER BY f.field', (recording_id, row['provider']))
            fields = {'deliveries': [self._delivery_field(reference) for reference in references]}
            result.append({'state': dict(row), 'fields': fields})
        return {'runtime': False, 'rows': result}

    @staticmethod
    def _delivery_field(row):
        return {'field': row['field'], 'delivery_id': row['delivery_id'],
                'reference': tuple(row[name] for name in ('raw_filename', 'raw_digest', 'raw_byte_size', 'raw_offset'))}

    def expiry_guard(self):
        """Keep writer ownership across the expiry slice and its separate commits."""
        return self._lock

    @synchronized
    def expiry_state(self):
        if not metadata_runtime.active(self.conn):
            return None
        row = self.conn.execute('SELECT value FROM settings WHERE key=?', (EXPIRY,)).fetchone()
        return json.loads(row[0])

    @synchronized
    def expiry_page(self, cursor, *, limit):
        rows = []
        for (recording,) in self.conn.execute('SELECT id FROM recordings WHERE id>? ORDER BY id LIMIT ?', (cursor, limit)).fetchall():
            conflicts = [dict(row) for row in self.conn.execute(
                f'SELECT {metadata_runtime.CONFLICT_COLUMNS} FROM metadata_conflicts WHERE runtime_id IS NOT NULL '
                'AND (recording_id=? OR entry_id IN (SELECT id FROM track_sources WHERE recording_id=?))', (recording, recording))]
            for conflict in conflicts:
                conflict['file'] = metadata_file_writes.reference(self.conn, 'conflicts', (conflict['id'],), conflict['revision'])
            sources = [dict(row) for row in self.conn.execute('SELECT s.source_key,s.revision FROM rec_musical_sources s '
                'JOIN rec_musical_links l ON l.source_key=s.source_key WHERE l.recording_id=?', (recording,))]
            rows.append({'recording_id': recording, 'conflicts': conflicts, 'sources': sources})
        return rows

    @synchronized
    def store_expiry_shard(self, shard):
        with self.conn.atomic():
            if type(shard) is not int or not 0 <= shard < 100:
                raise ValueError('Invalid metadata expiry shard')
            self.conn.execute("UPDATE settings SET value=json_set(value,'$.gc_shard',?) WHERE key=?", (shard, EXPIRY))

    @synchronized
    def store_conflict_revision(self, identity, revision, *, expected_revision, file_record):
        with self.conn.atomic():
            changed = self.conn.execute('UPDATE metadata_conflicts SET runtime_revision=? WHERE runtime_id=? AND runtime_revision=?',
                (revision, identity, expected_revision)).rowcount
            if changed != 1:
                raise metadata_observations.SubjectChanged('Metadata conflict changed during expiry')
            metadata_file_writes.complete(self.conn, file_record, kind='conflicts', owner=(identity,), revision=revision)

    @synchronized
    def store_expiry_cursor(self, cursor, after):
        with self.conn.atomic():
            self.conn.execute("UPDATE settings SET value=json_set(value,'$.gc_cursor',?,'$.gc_after',json(?)) WHERE key=?",
                              (cursor, json.dumps(metadata_runtime.clock(after)), EXPIRY))

    @synchronized
    def retire_expired_alternative_files(self, cutoff, *, limit):
        with self.conn.atomic():
            identities = [row[0] for row in self.conn.execute('SELECT f.file_address FROM collection_file_writes f '
                'JOIN enrichment_provider_config p ON p.metadata_provider_id=json_extract(f.metadata_owner_key,\'$[1]\') '
                'JOIN enrichment_provider_state s ON s.recording_id=(f.owner_id->>3) AND s.provider=p.provider '
                "WHERE f.kind='runtime-metadata' AND f.owner_id->>0='alternatives' AND f.selected=1 "
                "AND s.status<>'queued' AND s.at<=? ORDER BY f.file_address LIMIT ?", (cutoff, limit))]
            self.conn.executemany("UPDATE collection_file_writes SET selected=0 WHERE kind='runtime-metadata' "
                                  'AND file_address=?', [(identity,) for identity in identities])
            return identities

    @synchronized
    def retire_file_revisions(self, kind, owner, keep, *, limit=128):
        from yt_playlist.core.metadata_file_contract import owner_key, revision_key
        if kind not in ('native', 'conflicts') or not keep:
            raise ValueError('Metadata revision collection requires a current revision')
        with self.conn.atomic():
            identities = [row[0] for row in self.conn.execute(
                "SELECT file_address FROM collection_file_writes WHERE kind='runtime-metadata' "
                'AND owner_id->>0=? AND metadata_owner_key=? AND selected=1 '
                f'AND owner_id->>2 NOT IN ({",".join("?" for _ in keep)}) ORDER BY file_address LIMIT ?',
                (kind, owner_key(kind, owner), *(revision_key(revision) for revision in keep), limit))]
            self.conn.executemany("UPDATE collection_file_writes SET selected=0 WHERE kind='runtime-metadata' "
                                  'AND file_address=?', [(identity,) for identity in identities])
            return identities

    @synchronized
    def enrichment_cooldowns(self):
        return {r["provider"]: dict(r) for r in self.conn.execute(
            'SELECT provider,cooldown_failures failures,cooldown_retry_at retry_at,cooldown_error error '
            'FROM enrichment_provider_config WHERE cooldown_failures IS NOT NULL')}

    @synchronized
    def retry_status_inputs(self, provider, ready_sql):
        tracks = enrichment_tracks_sql('SELECT r.id,r.identity_key')
        row = self.conn.execute(
            "SELECT COUNT(*) OVER () AS count,s.error,MAX(s.retry_at,COALESCE(cfg.cooldown_retry_at,0)) retry_at "
            "FROM enrichment_provider_state s JOIN recordings r ON r.id=s.recording_id "
            "LEFT JOIN enrichment_provider_config cfg ON cfg.provider=s.provider "
            "WHERE s.status='retry' AND s.cycle=COALESCE(r.last_enriched_at,-1) "
            "AND (cfg.status IS NULL OR cfg.status='available') "
            f"AND (s.provider<>? OR EXISTS (SELECT 1 FROM ({tracks}) t "
            "WHERE " + ready_sql + ")) "
            "ORDER BY retry_at,s.at LIMIT 1", (provider,)).fetchone()
        return dict(row) if row else None

    @synchronized
    def artwork_state(self, entry_id):
        previous = self.conn.execute("SELECT artwork_failures AS failures FROM track_sources "
                                     "WHERE id=? AND artwork_status IS NOT NULL",
                                     (entry_id,)).fetchone()
        return dict(previous) if previous else None

    @synchronized
    def store_artwork_result(self, entry_id, release_ids, result):
        with self.conn.atomic(join=True):
            self.conn.execute('UPDATE track_sources SET artwork_release_ids=?,artwork_status=?,artwork_failures=?,'
                'artwork_retry_at=?,artwork_error=? WHERE id=?',
                (json.dumps(release_ids), *(result[key] for key in ('status', 'failures', 'retry_at', 'error')), entry_id))

    @synchronized
    def due_artwork_retries(self, now, limit=1):
        rows = self.conn.execute('SELECT id AS entry_id,artwork_release_ids AS release_ids_json,'
            'artwork_status AS status,artwork_failures AS failures,artwork_retry_at AS retry_at,artwork_error AS error '
            f"FROM track_sources e WHERE artwork_status='retry' AND artwork_retry_at<=? AND EXISTS ({_ENRICHMENT_ENTRY}) "
            'ORDER BY artwork_retry_at,id LIMIT ?', (now, limit)).fetchall()
        return [{**dict(row), 'release_ids': json.loads(row['release_ids_json'])} for row in rows]

    @synchronized
    def artwork_retry_status(self):
        row = self.conn.execute('SELECT COUNT(*) count,MIN(artwork_retry_at) retry_at '
            "FROM track_sources e WHERE artwork_status='retry' AND "
            f"EXISTS ({_ENRICHMENT_ENTRY})").fetchone()
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
                                  for row in self.conn.execute('SELECT provider,fingerprint,status FROM enrichment_provider_config WHERE status IS NOT NULL')},
                'frontiers': {provider: provider_frontiers.read(self.conn, provider) for provider in providers}}

    @synchronized
    def store_configuration_changes(self, changes, cutoff):
        with self.conn.atomic(join=True):
            for change in changes:
                provider, current = change['provider'], change['current']
                if change['replace']:
                    self.conn.execute("INSERT INTO enrichment_provider_config(provider,fingerprint,status) VALUES (?,?,?) "
                                      "ON CONFLICT(provider) DO UPDATE SET fingerprint=excluded.fingerprint,status=excluded.status",
                                      (provider,current["fingerprint"],current["status"]))
                if change['clear']:
                    provider_frontiers.clear(self.conn, provider)
                if change['restart']:
                    self.conn.execute('UPDATE enrichment_provider_config SET cooldown_failures=NULL,cooldown_retry_at=NULL,cooldown_error=NULL '
                                      'WHERE provider=?', (provider,))
                    provider_frontiers.start(self.conn, provider, cutoff, current['fingerprint'])

    @synchronized
    def refresh_inputs(self, provider, limit):
        refresh = provider_frontiers.read(self.conn, provider)
        configuration = self.conn.execute('SELECT fingerprint,status FROM enrichment_provider_config WHERE provider=? '
                                          'AND status IS NOT NULL', (provider,)).fetchone()
        runtime = metadata_runtime.active(self.conn)
        rows = []
        if refresh is not None:
            attempts = ('0 last_attempt,0 completed_attempt' if runtime else
                '(SELECT COALESCE(MAX(p.id),0) FROM provider_attempts p WHERE p.recording_id=r.id AND p.provider=:provider) last_attempt,'
                '(SELECT COALESCE(MAX(p.id),0) FROM provider_attempts p WHERE p.recording_id=r.id AND p.provider=:provider '
                "AND p.status<>'retry') completed_attempt")
            rows = [dict(row) for row in self.conn.execute('SELECT r.id,r.first_enriched_at,s.at,s.status,' + attempts +
                " FROM recordings r LEFT JOIN enrichment_provider_state s ON s.recording_id=r.id AND s.provider=:provider AND s.status<>'queued' "
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
            self.conn.execute('DELETE FROM enrichment_provider_state WHERE provider=:provider AND queued=0 '
                              'AND recording_id IN (SELECT value FROM json_each(:ids))', params)
            self.conn.execute("UPDATE enrichment_provider_state SET cycle=-1,at=0,status='queued',failures=0,retry_at=0,error='',"
                              "credit_delivery_id=NULL,field_links='{}' WHERE provider=:provider AND queued=1 "
                              'AND recording_id IN (SELECT value FROM json_each(:ids))', params)
            self.conn.executemany(QUEUE_PROVIDER + 'VALUES (?,?' + QUEUED_VALUES + ')' + SET_QUEUED,
                                  [(identity, provider) for identity in jobs])
            provider_frontiers.advance(self.conn, provider, refresh, cursor)

    @snapshot_read
    def provider_refresh_pending(self, provider):
        return provider_frontiers.read(self.conn,provider) is not None

    @snapshot_read
    def missing_provider_coverage(self, provider, *, after_id, limit):
        if metadata_runtime.active(self.conn):
            return [row[0] for row in self.conn.execute('SELECT r.id FROM recordings r '
                "WHERE r.id>? AND first_enriched_at IS NOT NULL AND EXISTS (SELECT 1 FROM enrichment_provider_state s "
                "WHERE s.recording_id=r.id AND s.status<>'queued') "
                'AND NOT EXISTS (SELECT 1 FROM enrichment_provider_state s WHERE s.recording_id=r.id '
                "AND s.provider=? AND (s.status<>'retry' OR s.queued=1)) "
                f'AND EXISTS ({_ENRICHMENT_SUBJECT}) ORDER BY r.id LIMIT ?',
                (after_id, provider, limit))]
        return [row[0] for row in self.conn.execute(
            'SELECT r.id FROM recordings r WHERE r.id>? AND first_enriched_at IS NOT NULL '
            'AND EXISTS (SELECT 1 FROM provider_attempts p WHERE p.recording_id=r.id) '
            'AND NOT EXISTS (SELECT 1 FROM provider_attempts p WHERE p.recording_id=r.id '
            "AND p.provider=? AND p.status<>'retry') AND NOT EXISTS (SELECT 1 FROM enrichment_provider_state s "
            'WHERE s.recording_id=r.id AND s.provider=? AND s.queued=1) '
            f'AND EXISTS ({_ENRICHMENT_SUBJECT}) ORDER BY r.id LIMIT ?', (after_id, provider, provider, limit))]

    @synchronized
    def admit_provider_coverage(self, provider, current, candidates):
        with self.conn.atomic():
            if not self._provider_matches(provider, current):
                return False
            if metadata_runtime.active(self.conn):
                self.conn.execute(QUEUE_PROVIDER + 'SELECT t.recording_id,:provider' + QUEUED_VALUES +
                    f' FROM ({_ENRICHMENT_SELECTED}) t WHERE t.first_enriched_at IS NOT NULL AND EXISTS '
                    "(SELECT 1 FROM enrichment_provider_state s WHERE s.recording_id=t.recording_id AND s.status<>'queued') AND NOT EXISTS "
                    "(SELECT 1 FROM enrichment_provider_state s WHERE s.recording_id=t.recording_id AND s.provider=:provider AND s.status<>'retry')" +
                    SET_QUEUED,
                    dict(ids=json.dumps(candidates), provider=provider))
                return True
            self.conn.execute(QUEUE_PROVIDER + 'SELECT t.recording_id,:provider' + QUEUED_VALUES +
                f' FROM ({_ENRICHMENT_SELECTED}) t '
                'WHERE t.first_enriched_at IS NOT NULL '
                'AND EXISTS (SELECT 1 FROM provider_attempts p WHERE p.recording_id=t.recording_id) '
                'AND NOT EXISTS (SELECT 1 FROM provider_attempts p WHERE p.recording_id=t.recording_id '
                "AND p.provider=:provider AND p.status<>'retry')" + SET_QUEUED,
                dict(ids=json.dumps(candidates), provider=provider))
        return True

    @synchronized
    def enrichment_provider_jobs(self, recording_id):
        return [r[0] for r in self.conn.execute(
            "SELECT s.provider FROM enrichment_provider_state s JOIN enrichment_provider_config c "
            "ON c.provider=s.provider WHERE s.recording_id=? AND s.queued=1 AND c.status='available'", (recording_id,))]

    @synchronized
    def coverage_inputs(self, recording_id):
        return [dict(row) for row in self.conn.execute("SELECT c.provider,c.status configured,NULLIF(s.status,'queued') status,COALESCE(s.queued,0) pending "
            'FROM enrichment_provider_config c LEFT JOIN enrichment_provider_state s ON s.provider=c.provider AND s.recording_id=? '
            'WHERE c.status IS NOT NULL', (recording_id,))]

    @synchronized
    def attempt_inputs(self, identity_key, video_id=None):
        key = catalog.resolve_key(self.conn,identity_key,video_id)
        if metadata_runtime.active(self.conn):
            recording = self.conn.execute('SELECT id FROM recordings WHERE identity_key=?', (key,)).fetchone()
            if recording is None:
                return {'runtime': True, 'subject': None, 'rows': []}
            return self.provider_state_inputs(recording[0], include_previous=True)
        rows = self.conn.execute('WITH relevant AS (SELECT p.*,ROW_NUMBER() OVER '
            '(PARTITION BY p.provider ORDER BY p.at DESC,p.id DESC) AS latest FROM provider_attempts p WHERE '
            '(p.recording_id=(SELECT id FROM recordings WHERE identity_key=?) OR p.input_video_id IN '
            '(SELECT y.video_id FROM track_sources y JOIN recordings r ON r.id=y.recording_id WHERE r.identity_key=?)) '
            'AND (? IS NULL OR p.input_video_id=?)) SELECT * FROM relevant WHERE latest=1 ORDER BY at,id',
            (key, key, video_id, video_id))
        result = []
        for row in rows:
            references = self.conn.execute('SELECT f.field,f.delivery_id,l.raw_filename,l.raw_digest,l.raw_byte_size,l.raw_offset '
                'FROM ' + provider_credit_fields.ATTEMPT_SOURCE + ' f '
                'JOIN metadata_delivery_locations l ON l.id=f.delivery_id WHERE f.attempt_id=? ORDER BY f.field', (row['id'],))
            result.append({'provider': row['provider'], 'at': row['at'], 'status': row['status'],
                           'fields': {'deliveries': [self._delivery_field(reference) for reference in references]}})
        return {'runtime': False, 'rows': result}

    @synchronized
    def store_enrichment_log(self, inputs, run_id, provider, field, value, at, *, scope, scheme, identifiers,
                             delivery_id, observation_id):
        entry, recording, video = inputs['entry_id'], inputs['recording_id'], inputs['video_id']
        with self.conn.atomic():
            current = self.conn.execute('SELECT recording_id,video_id FROM track_sources WHERE id=?', (entry,)).fetchone()
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
