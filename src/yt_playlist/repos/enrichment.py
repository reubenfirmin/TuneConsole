"""Enrichment provider state and conflict persistence."""
import json
from contextlib import contextmanager

from yt_playlist.core import metadata, metadata_evidence, metadata_conflicts, metadata_hints
from yt_playlist.core import metadata_runtime, metadata_values, genre_classifications
from yt_playlist.repos.base import Repo, synchronized, snapshot_read
from yt_playlist.repos import metadata_file_writes

class EnrichmentRepo(Repo):
    @synchronized
    def genre_refresh_input(self, recording_id):
        entry = self.conn.execute('SELECT id FROM track_sources WHERE recording_id=? ORDER BY id LIMIT 1', (recording_id,)).fetchone()
        if entry is None:
            return None
        return {'entry_id': entry[0], 'source': metadata.selected_source(self.conn, entry[0], 'genre'),
                'attempts': not metadata_runtime.active(self.conn)}

    @snapshot_read
    def provider_inputs(self, entry, provider):
        subject = self.conn.execute('SELECT recording_id,video_id FROM track_sources WHERE id=?', (entry,)).fetchone()
        if subject is None:
            raise ValueError('Unknown provider input')
        runtime = metadata_runtime.active(self.conn)
        state = self.conn.execute("SELECT at FROM enrichment_provider_state WHERE recording_id=? AND provider=? AND status<>'queued'",
                                  (subject['recording_id'], provider)).fetchone()
        return {'entry_id': entry, **dict(subject), 'layout': 'runtime' if runtime else 'evidence',
                'provider_at': state['at'] if state else None, 'attempts': not runtime,
                'genre': genre_classifications.read(self.conn, 'recordings', subject['recording_id']),
                'sources': metadata.selected_sources(self.conn, entry,
                    (*metadata_values.ITEM_FIELDS, *metadata_values.RECORDING_FIELDS))}

    @snapshot_read
    def reuse_inputs(self, recording, provider, fields):
        if not fields:
            return {}
        rows = self.conn.execute('SELECT f.key concept,o.* FROM enrichment_provider_state s,json_each(s.field_links) f '
            'JOIN metadata_observations o ON o.id=f.value->>1 WHERE s.recording_id=? AND s.provider=? AND f.key IN (' +
            ','.join('?' for _ in fields) + ')', (recording, provider, *fields))
        return {row['concept']: {**dict(row), 'value': metadata_evidence.value_from_row(row)} for row in rows}

    @synchronized
    def retire_entry_hints(self, entry, field):
        with self.conn.atomic():
            metadata_hints.retire_entry(self.conn, entry, field)

    @snapshot_read
    def hint_inputs(self, entry, fields):
        track = self.conn.execute('SELECT * FROM catalog_tracks WHERE id=?', (entry,)).fetchone()
        if track is None:
            return None
        layout = 'runtime' if metadata_runtime.active(self.conn) else 'evidence'
        result = {}
        for field in fields:
            if field not in (*metadata_values.ITEM_FIELDS, *metadata_values.RECORDING_FIELDS):
                continue
            hints = []
            if layout != 'runtime':
                rows = metadata_hints.active(self.conn, entry, track['recording_id'], field)
                for row in rows:
                    value = metadata_evidence.value_from_row(row)
                    hints.append({'entry_id': row['entry_id'], 'source': row['source'], 'value': value})
            result[field] = {'current': track[field], 'source': metadata.selected_source(self.conn, entry, field), 'hints': hints}
        return {'entry_id': entry, 'layout': layout, 'fields': result}

    @synchronized
    def retire_hints(self, field, keys):
        if not keys:
            return
        with self.conn.atomic():
            metadata_hints.retire(self.conn, field, keys)

    @contextmanager
    def conflict_transaction(self):
        with self._lock:
            with self.conn.atomic():
                self.conn.execute('UPDATE recordings SET id=id WHERE 0')
                yield

    @contextmanager
    def file_reservation(self):
        with self._lock:
            if self.conn.in_transaction:
                raise ValueError('Metadata file reservation requires its own transaction')
            with self.conn.atomic():
                yield

    @snapshot_read
    def conflict_inputs(self, track_id, field=None):
        subject = self.conn.execute('SELECT recording_id FROM track_sources WHERE id=?', (track_id,)).fetchone()
        if subject is None:
            return None
        recording = subject[0]
        result = []
        if metadata_runtime.active(self.conn):
            layout = 'runtime'
            rows = self.conn.execute(f'SELECT {metadata_runtime.CONFLICT_COLUMNS} FROM metadata_conflicts '
                'WHERE runtime_id IS NOT NULL AND (entry_id=? OR recording_id=?)' +
                (' AND field=?' if field is not None else '') + ' ORDER BY runtime_id',
                (track_id, recording, metadata_conflicts.field_name(field)) if field is not None else (track_id, recording))
            for row in rows:
                result.append({**dict(row),
                    'evidence': {'reference': (row['id'], row['revision']), 'updated_at': row['updated_at'],
                        'file': metadata_file_writes.reference(self.conn, 'conflicts', (row['id'],), row['revision'])}})
        else:
            layout = 'evidence'
            for row in metadata_conflicts.for_entry(self.conn, track_id, field):
                result.append({**dict(row), 'status': {'unresolved': 0, 'user_resolved': 1, 'provider_converged': 2}[row['status']],
                               'evidence': {'candidates': metadata_conflicts.candidates(self.conn, row['id'])}})
        return {'entry_id': track_id, 'recording_id': recording, 'layout': layout, 'conflicts': result}

    @synchronized
    def alternative_references(self, recording_id, providers):
        if not providers:
            return {}
        return {row['provider']: dict(row) | {'file': metadata_file_writes.reference(self.conn, 'alternatives',
                    (recording_id, row['provider_id']), float(row['at']))} for row in self.conn.execute(
            'SELECT s.provider,p.metadata_provider_id provider_id,s.at FROM enrichment_provider_state s '
            'JOIN enrichment_provider_config p ON p.provider=s.provider AND p.metadata_provider_id IS NOT NULL '
            "WHERE s.recording_id=? AND s.status<>'queued' "
            f'AND s.provider IN ({",".join("?" for _ in providers)})', (recording_id, *providers))}

    @synchronized
    def reserve_conflict_identity(self):
        return metadata_file_writes.next_conflict_id(self.conn)

    @synchronized
    def reserve_file(self, record):
        return metadata_file_writes.reserve(self.conn, record)

    @synchronized
    def store_conflict(self, inputs, field, candidates, status, at, *, scope, identity=None, revision=None, file_record=None):
        entry, recording, layout = inputs['entry_id'], inputs['recording_id'], inputs['layout']
        with self.conn.atomic(join=True):
            if layout == 'runtime':
                if file_record is None:
                    raise ValueError('Runtime metadata conflict requires its reserved file')
                metadata_file_writes.complete(self.conn, file_record, kind='conflicts', owner=(identity,), revision=revision)
                metadata_runtime.store_conflict(self.conn, entry, recording, field, scope, identity, status, revision, at)
            else:
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
                metadata_conflicts.save_candidates(self.conn, identity, observations)

    @synchronized
    def mark_converged(self, layout, identities, at):
        column = 'runtime_id' if layout == 'runtime' else 'id'
        self.conn.executemany(f"UPDATE metadata_conflicts SET status='provider_converged',updated_at=? WHERE {column}=?",
                              ((at, identity) for identity in identities))

    @synchronized
    def mark_resolved(self, track_id, field, observation, at):
        with self.conn.atomic(join=True):
            if metadata_runtime.active(self.conn):
                conflict = metadata_runtime.conflict(self.conn, track_id, field)
                if conflict:
                    self.conn.execute("UPDATE metadata_conflicts SET status='user_resolved',updated_at=? WHERE runtime_id=?",
                                      (at, conflict['id']))
                return
            metadata_conflicts.resolve(self.conn, track_id, field, observation, now=at)

    @snapshot_read
    def resolution_inputs(self, track_id, field, *, column):
        row = self.conn.execute(
            f"SELECT c.candidates, t.{column} current_value FROM enrichment_conflict c "
            "JOIN catalog_tracks t ON t.id=c.track_id WHERE c.track_id=? AND c.field=?",
            (track_id, field)).fetchone()
        if row is None:
            return None
        if metadata_runtime.active(self.conn):
            conflict = metadata_runtime.conflict(self.conn, track_id, field)
            evidence = {'reference': (conflict['id'], conflict['revision']), 'updated_at': conflict['updated_at'],
                'file': metadata_file_writes.reference(self.conn, 'conflicts', (conflict['id'],), conflict['revision'])}
        else:
            evidence = {'candidates': json.loads(row['candidates'])}
        return {'entry_id': track_id, 'current': row['current_value'], 'evidence': evidence}
