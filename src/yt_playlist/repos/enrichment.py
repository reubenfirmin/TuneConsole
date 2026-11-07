"""Enrichment provider state and conflict persistence."""
import json
import time

from yt_playlist.core import catalog, metadata, metadata_evidence, metadata_conflicts, metadata_attempts
from yt_playlist.core import metadata_runtime
from yt_playlist.repos.base import Repo, synchronized, snapshot_read

class EnrichmentRepo(Repo):
    @synchronized
    def apply_provider_fields(self, track_id, provider, fields, *, providers=(), delivery_id=None):
        """Select typed provider findings below explicit user overrides."""
        track_id = catalog.resolve_entry(self.conn,track_id)
        with self.conn:
            return metadata.apply_provider(self.conn,track_id,provider,fields,providers,delivery_id=delivery_id)

    @synchronized
    def reuse_provider_fields(self, entry_id, provider, fields, *, providers=(), now=None, select=True):
        entry_id = catalog.resolve_entry(self.conn, entry_id)
        with self.conn:
            if metadata_runtime.active(self.conn):
                data = metadata_runtime.alternatives(self.conn, metadata_runtime.subject(self.conn, entry_id)[0], provider, now=now)
                scoped = metadata_runtime.scoped_fields(self.conn, entry_id, data)
                fields = {field: value for field, value in fields.items() if field in scoped and scoped[field] == value}
                if select:
                    metadata.apply_provider(self.conn, entry_id, provider, fields, providers, now=now)
                return None
            if not metadata_attempts.available(self.conn):
                if select:
                    metadata.apply_provider(self.conn, entry_id, provider, fields, providers, now=now)
                return None
            if not self.conn.in_transaction:
                self.conn.execute('BEGIN IMMEDIATE')
            observations = metadata_attempts.reusable_evidence(self.conn, entry_id, provider, fields, now=now)
            if select:
                metadata.apply_provider(self.conn, entry_id, provider, fields, providers, now=now,
                                        supplying_observations=observations)
            return observations

    @synchronized
    def replace_catalog_hints(self, track_id, fields):
        """Replace imported hints, retaining user choices and superseded hint evidence."""
        with self.conn:
            catalog.replace_hints(self.conn,track_id,fields)

    @synchronized
    def upsert_conflict(self, track_id, field, candidates, now=None, *, refreshed_providers=None) -> None:
        """Refresh candidates; a resolved conflict reopens only for a newly offered value."""
        track_id = catalog.resolve_entry(self.conn, track_id)
        if metadata_runtime.active(self.conn):
            with self.conn:
                if not self.conn.in_transaction:
                    self.conn.execute('BEGIN IMMEDIATE')
                metadata_runtime.upsert_conflict(self.conn, track_id, field, candidates, now=now,
                                                 refreshed_providers=refreshed_providers)
            return
        if metadata_evidence.available(self.conn):
            with self.conn:
                if not self.conn.in_transaction:
                    self.conn.execute('BEGIN IMMEDIATE')
                metadata_conflicts.upsert(self.conn, track_id, field, candidates, now=now,
                                          refreshed_providers=refreshed_providers)
            return
        row = self.conn.execute(
            "SELECT candidates, resolved FROM enrichment_conflict WHERE track_id=? AND field=?",
            (track_id, field)).fetchone()
        if row is not None and refreshed_providers is not None:
            current_providers = {c['provider'] for c in candidates}
            # Skipped/failed providers retain evidence until they successfully refresh it.
            candidates = [*candidates, *(c for c in json.loads(row['candidates'])
                if c['provider'] not in refreshed_providers and c['provider'] not in current_providers)]
        payload = json.dumps(candidates)
        new_values = {str(c["value"]) for c in candidates}
        # resolved=2 is provider convergence; any later disagreement reopens it.
        reopen = row is not None and row["resolved"] == 2
        if row is not None and row["resolved"] == 1:
            old_values = {str(c["value"]) for c in json.loads(row["candidates"])}
            reopen = bool(new_values - old_values)
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
        """Retire conflicts only after every previous candidate provider refreshed the field.

        Agreeing nonempty evidence preserves history and explicit selections. Recording facts
        can converge through another associated input; release/item facts cannot.
        """
        track_id = catalog.resolve_entry(self.conn, track_id)
        if metadata_runtime.active(self.conn):
            with self.conn:
                metadata_runtime.converge(self.conn, track_id, evidence, now=now)
            return
        if metadata_evidence.available(self.conn):
            with self.conn:
                if not self.conn.in_transaction:
                    self.conn.execute('BEGIN IMMEDIATE')
                metadata_conflicts.converge(self.conn, track_id, evidence, now=now)
            return
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
