"""Enrichment provider state and conflict persistence."""
import json
from contextlib import contextmanager

from yt_playlist.core import catalog, metadata, metadata_evidence, metadata_conflicts, metadata_attempts
from yt_playlist.core import metadata_runtime, metadata_values, genre_classifications
from yt_playlist.repos.base import Repo, synchronized, snapshot_read

class EnrichmentRepo(Repo):
    @snapshot_read
    def provider_inputs(self, entry, provider):
        entry = catalog.resolve_entry(self.conn, entry)
        subject = self.conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()
        if subject is None:
            raise ValueError('Unknown provider input')
        layout = ('runtime' if metadata_runtime.active(self.conn) else
                  'evidence' if metadata_evidence.available(self.conn) else 'legacy')
        state = self.conn.execute('SELECT at FROM enrichment_provider_state WHERE recording_id=? AND provider=?',
                                  (subject['recording_id'], provider)).fetchone()
        return {'entry_id': entry, **dict(subject), 'layout': layout,
                'provider_at': state['at'] if state else None, 'attempts': metadata_attempts.available(self.conn),
                'genre': genre_classifications.read(self.conn, 'recordings', subject['recording_id']),
                'sources': {field: metadata.selected_source(self.conn, entry, field)
                            for field in (*metadata_values.ITEM_FIELDS, *metadata_values.RECORDING_FIELDS)}}

    @snapshot_read
    def reuse_inputs(self, recording, provider, fields):
        if not fields:
            return {}
        rows = self.conn.execute('SELECT s.field concept,o.* FROM provider_state_fields s JOIN metadata_observations o '
            'ON o.id=s.observation_id WHERE s.recording_id=? AND s.provider=? AND s.field IN (' +
            ','.join('?' for _ in fields) + ')', (recording, provider, *fields))
        return {row['concept']: {**dict(row), 'value': metadata_evidence.value_from_row(row)} for row in rows}

    @synchronized
    def store_finding(self, entry, recording, field, provider, value, at):
        with self.conn.atomic():
            self.conn.execute('INSERT INTO metadata_findings(entry_id,recording_id,field,provider,value_json,observed_at) '
                              'VALUES (?,?,?,?,?,?)', (entry, recording, field, provider, json.dumps(value), at))

    @synchronized
    def retire_entry_hints(self, layout, entry, field):
        table = {'evidence': 'metadata_hints', 'legacy': 'catalog_field_hints'}[layout]
        with self.conn.atomic():
            self.conn.execute(f'UPDATE {table} SET active=0 WHERE entry_id=? AND field=?', (entry, field))

    @snapshot_read
    def hint_inputs(self, entry, fields):
        entry = catalog.resolve_entry(self.conn, entry)
        track = self.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone()
        if track is None:
            return None
        layout = ('runtime' if metadata_runtime.active(self.conn) else
                  'evidence' if metadata_evidence.available(self.conn) else 'legacy')
        result = {}
        for field in fields:
            if field not in (*metadata_values.ITEM_FIELDS, *metadata_values.RECORDING_FIELDS):
                continue
            hints = []
            if layout != 'runtime':
                table = 'metadata_hints' if layout == 'evidence' else 'catalog_field_hints'
                rows = self.conn.execute('SELECT h.*' + (',o.value_kind,o.text_value,o.number_value,o.genre_key,o.unit,o.resolution_reason'
                    if layout == 'evidence' else '') + f' FROM {table} h ' +
                    ('JOIN metadata_observations o ON o.id=h.observation_id ' if layout == 'evidence' else '') +
                    'JOIN catalog_entries e ON e.id=h.entry_id WHERE h.active=1 AND h.field=? AND ' +
                    ('e.recording_id=?' if field in metadata_values.RECORDING_FIELDS else 'e.id=?'),
                    (field, track['recording_id'] if field in metadata_values.RECORDING_FIELDS else entry))
                for row in rows:
                    if layout == 'evidence':
                        value = metadata_evidence.value_from_row(row)
                    else:
                        raw = json.loads(row['value_json'])
                        value = metadata_values.Value('value', number=raw) if isinstance(raw, (int, float)) else metadata_values.Value('value', text=raw)
                    hints.append({'entry_id': row['entry_id'], 'source': row['source'], 'value': value})
            result[field] = {'current': track[field], 'source': metadata.selected_source(self.conn, entry, field), 'hints': hints}
        return {'entry_id': entry, 'layout': layout, 'fields': result}

    @synchronized
    def retire_hints(self, layout, field, keys):
        if not keys:
            return
        table = {'evidence': 'metadata_hints', 'legacy': 'catalog_field_hints'}[layout]
        with self.conn.atomic():
            self.conn.executemany(f'UPDATE {table} SET active=0 WHERE entry_id=? AND field=? AND source=?',
                                  [(entry, field, source) for entry, source in keys])

    @contextmanager
    def conflict_transaction(self):
        with self._lock:
            with self.conn.atomic():
                self.conn.execute('UPDATE catalog_entries SET id=id WHERE 0')
                yield

    @snapshot_read
    def conflict_inputs(self, track_id, field=None):
        track_id = catalog.resolve_entry(self.conn, track_id)
        subject = self.conn.execute('SELECT recording_id FROM catalog_entries WHERE id=?', (track_id,)).fetchone()
        if subject is None:
            return None
        recording = subject[0]
        result = []
        if metadata_runtime.active(self.conn):
            layout = 'runtime'
            rows = self.conn.execute('SELECT * FROM metadata_runtime_conflicts WHERE (entry_id=? OR recording_id=?)' +
                (' AND field=?' if field is not None else '') + ' ORDER BY id',
                (track_id, recording, metadata_runtime.code(field)) if field is not None else (track_id, recording))
            for row in rows:
                result.append({**dict(row), 'field': metadata_runtime.FIELDS[row['field'] - 1],
                    'evidence': {'reference': (row['id'], row['revision']), 'updated_at': row['updated_at']}})
        elif metadata_evidence.available(self.conn):
            layout = 'evidence'
            for row in metadata_conflicts.for_entry(self.conn, track_id, field):
                result.append({**dict(row), 'status': {'unresolved': 0, 'user_resolved': 1, 'provider_converged': 2}[row['status']],
                               'evidence': {'candidates': metadata_conflicts.candidates(self.conn, row['id'])}})
        else:
            layout = 'legacy'
            concept = 'year' if field == 'mb_year' else field
            rows = self.conn.execute('SELECT * FROM enrichment_conflict WHERE ' +
                ('track_id=? AND field=?' if field is not None else
                 "(track_id=? OR (field_scope='recording' AND recording_id=?))"),
                (track_id, concept) if field is not None else (track_id, recording))
            for row in rows:
                result.append({**dict(row), 'entry_id': row['track_id'], 'status': row['resolved'],
                    'field': 'mb_year' if row['field'] == 'year' else row['field'],
                    'evidence': {'candidates': json.loads(row['candidates'])}})
        return {'entry_id': track_id, 'recording_id': recording, 'layout': layout, 'conflicts': result}

    @synchronized
    def alternative_references(self, recording_id, providers):
        if not providers:
            return {}
        return {row['provider']: dict(row) for row in self.conn.execute(
            'SELECT s.provider,p.id provider_id,s.at FROM enrichment_provider_state s '
            'JOIN metadata_runtime_providers p ON p.name=s.provider WHERE s.recording_id=? '
            f'AND s.provider IN ({",".join("?" for _ in providers)})', (recording_id, *providers))}

    @synchronized
    def reserve_conflict_identity(self):
        return metadata_runtime.next_identity(self.conn, 'metadata_runtime_conflicts')

    @synchronized
    def store_conflict(self, inputs, field, candidates, status, at, *, scope, identity=None, revision=None):
        entry, recording, layout = inputs['entry_id'], inputs['recording_id'], inputs['layout']
        with self.conn.atomic(join=True):
            if layout == 'runtime':
                self.conn.execute('INSERT INTO metadata_runtime_conflicts VALUES (?,?,?,?,?,?,?) '
                    'ON CONFLICT(id) DO UPDATE SET status=excluded.status,revision=excluded.revision,updated_at=excluded.updated_at',
                    (identity, recording if scope == 'recording' else None, None if scope == 'recording' else entry,
                     metadata_runtime.code(field), status, revision, at))
            elif layout == 'evidence':
                observations = []
                for candidate in candidates:
                    observation = candidate['observation_id']
                    prepared = candidate.get('prepared')
                    if prepared is None:
                        observations.append(observation)
                        continue
                    row = self.conn.execute('SELECT o.*,COALESCE(d.provider,o.legacy_source) provider '
                        'FROM metadata_observations o LEFT JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE o.id=?',
                        (observation,)).fetchone()
                    if (row is None or row['provider'] != candidate['provider'] or row['field'] != field
                            or row['field_scope'] != scope or row['recording_id'] != recording
                            or scope != 'recording' and row['entry_id'] != entry
                            or row['origin'] not in ('provider', 'selection_transfer')
                            or prepared is not None and (row['value_kind'], row['text_value'], row['number_value'], row['unit'])
                               != (prepared.kind, prepared.text, prepared.number, prepared.unit)):
                        raise ValueError('Metadata candidate has different evidence, subject, field or provider')
                    observations.append(observation)
                stored_status = ('unresolved', 'user_resolved', 'provider_converged')[status]
                if identity is None:
                    identity = self.conn.execute('INSERT INTO metadata_conflicts(entry_id,recording_id,field_scope,field,status,updated_at) '
                        'VALUES (?,?,?,?,?,?)', (None if scope == 'recording' else entry,
                        recording if scope == 'recording' else None, scope, field, stored_status, at)).lastrowid
                else:
                    self.conn.execute('UPDATE metadata_conflicts SET status=?,updated_at=? WHERE id=?', (stored_status, at, identity))
                    self.conn.execute('DELETE FROM metadata_conflict_candidates WHERE conflict_id=?', (identity,))
                self.conn.executemany('INSERT OR IGNORE INTO metadata_conflict_candidates(conflict_id,observation_id,position) VALUES (?,?,?)',
                    ((identity, observation, position) for position, observation in enumerate(observations)))
            else:
                payload = [{key: value for key, value in candidate.items() if key != 'prepared'} for candidate in candidates]
                self.conn.execute('INSERT INTO enrichment_conflict(track_id,field,candidates,resolved,updated_at,recording_id,input_video_id,field_scope) '
                    'VALUES (?,?,?,?,?,?,(SELECT video_id FROM catalog_entries WHERE id=?),?) '
                    'ON CONFLICT(track_id,field) DO UPDATE SET candidates=excluded.candidates,resolved=excluded.resolved,'
                    'updated_at=excluded.updated_at,recording_id=excluded.recording_id,input_video_id=excluded.input_video_id,field_scope=excluded.field_scope',
                    (entry, 'year' if field == 'mb_year' else field, json.dumps(payload), status, at, recording, entry, scope))

    @synchronized
    def mark_converged(self, layout, identities, at):
        table, column, value = {'runtime': ('metadata_runtime_conflicts', 'status', 2),
                               'evidence': ('metadata_conflicts', 'status', 'provider_converged'),
                               'legacy': ('enrichment_conflict', 'resolved', 2)}[layout]
        self.conn.executemany(f'UPDATE {table} SET {column}=?,updated_at=? WHERE id=?',
                              ((value, at, identity) for identity in identities))

    @synchronized
    def mark_resolved(self, track_id, field, value, observation, at):
        track_id = catalog.resolve_entry(self.conn, track_id)
        with self.conn.atomic(join=True):
            if metadata_runtime.active(self.conn):
                conflict = metadata_runtime.conflict(self.conn, track_id, field)
                if conflict:
                    self.conn.execute('UPDATE metadata_runtime_conflicts SET status=1,updated_at=? WHERE id=?',
                                      (at, conflict['id']))
                return
            if metadata_evidence.available(self.conn):
                metadata_conflicts.resolve(self.conn, track_id, field, observation, now=at)
                return
            self.conn.execute(
                "UPDATE enrichment_conflict SET resolved=1, resolved_value=?, updated_at=? "
                "WHERE track_id=? AND field=?",
                (None if value is None else str(value), at, track_id, field))

    @snapshot_read
    def resolution_inputs(self, track_id, field, *, column):
        track_id = catalog.resolve_entry(self.conn, track_id)
        row = self.conn.execute(
            f"SELECT c.candidates, t.{column} current_value FROM enrichment_conflict c "
            "JOIN catalog_tracks t ON t.id=c.track_id WHERE c.track_id=? AND c.field=?",
            (track_id, field)).fetchone()
        if row is None:
            return None
        if metadata_runtime.active(self.conn):
            conflict = metadata_runtime.conflict(self.conn, track_id, field)
            evidence = {'reference': (conflict['id'], conflict['revision']), 'updated_at': conflict['updated_at']}
        else:
            evidence = {'candidates': json.loads(row['candidates'])}
        return {'entry_id': track_id, 'current': row['current_value'], 'evidence': evidence}
