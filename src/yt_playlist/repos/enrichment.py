"""Canonical enrichment metadata and conflict mutation.

Provider precedence and explicit choices use core.metadata/catalog policy. This owner keeps
the writable-field whitelist and conflict convergence/reopening policy. A user resolution
validates its candidate, writes the chosen field and resolves the conflict under one Store
lock and transaction; reporting never owns part of that write.
"""
import json
import time

from yt_playlist.core import catalog, metadata
from yt_playlist.repos.base import Repo, synchronized

# Concept -> (canonical column, caster), shared with read-only review projections.
FIELD_COLUMN = {
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
    def replace_catalog_hints(self, track_id, fields):
        """Replace imported hints, retaining user choices and superseded hint evidence."""
        with self.conn:
            catalog.replace_hints(self.conn,track_id,fields)

    @synchronized
    def upsert_conflict(self, track_id, field, candidates, now=None, *, refreshed_providers=None) -> None:
        """Refresh candidates; a resolved conflict reopens only for a newly offered value."""
        track_id = catalog.resolve_entry(self.conn, track_id)
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
        """Choose a whitelisted canonical field without committing the caller's transaction."""
        track_id = catalog.resolve_entry(self.conn, track_id)
        spec = FIELD_COLUMN.get(field)
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
        """Write the user's choice and mark the conflict resolved in one transaction."""
        track_id = catalog.resolve_entry(self.conn, track_id)
        at = time.time() if now is None else now
        with self.conn:
            self._choose_track_field(track_id,field,value,now=at)
            self.conn.execute(
                "UPDATE enrichment_conflict SET resolved=1, resolved_value=?, updated_at=? "
                "WHERE track_id=? AND field=?",
                (None if value is None else str(value), at, track_id, field))

    @synchronized
    def resolve_enrichment_candidate(self, track_id, field, value, now=None) -> bool:
        """Accept a candidate or the saved value; validate and resolve under the same lock."""
        track_id = catalog.resolve_entry(self.conn, track_id)
        spec = FIELD_COLUMN.get(field)
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
