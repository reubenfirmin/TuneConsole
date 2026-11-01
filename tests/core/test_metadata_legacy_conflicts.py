"""Historical conflict conversion keeps usable values and exact originals."""
import json

import pytest

from yt_playlist.core import metadata_deliveries, metadata_legacy, metadata_native
from yt_playlist.core.migrations import migrate, registry
from yt_playlist.core.migrations import (
    v074_artist_credits, v075_metadata_deliveries, v080_metadata_evidence,
)
from yt_playlist.core.migrations.runner import Context
from yt_playlist.core.store import Store


@pytest.fixture
def legacy_store(tmp_path):
    """Retain the deliberate predecessor even after the draft is registered."""
    store = Store(tmp_path / 'legacy-conflicts.db')
    try:
        migrate(store.conn, registry=registry.MIGRATIONS[:69])
        with store.conn:
            store.conn.execute('BEGIN IMMEDIATE')
            for module in (v074_artist_credits, v075_metadata_deliveries):
                module.apply(store.conn, Context(4))
        store._bind_repositories()
        yield store
    finally:
        store.conn.close()


def test_numeric_legacy_conflict_candidates_keep_values_and_original(legacy_store):
    store = legacy_store
    entry = store.tracks.upsert_track('review-legacy-conflict', 'Song', 'Artist', None, 200)
    recording, = store.conn.execute(
        'SELECT recording_id FROM catalog_entries WHERE id=?', (entry,)).fetchone()
    candidates = json.dumps([
        {'provider': 'integer-provider', 'value': 120},
        {'provider': 'decimal-provider', 'value': 120.5},
        {'provider': 'old-text-provider', 'value': '130'},
        {'provider': 'malformed-provider', 'value': 'not a number'},
        {'provider': 'boolean-provider', 'value': True},
    ])
    with store.conn:
        legacy_id = store.conn.execute(
            'INSERT INTO enrichment_conflict(track_id,field,candidates,resolved,updated_at,'
            'recording_id,input_video_id,field_scope) VALUES (?,\'bpm\',?,0,20,?,?,\'recording\')',
            (entry, candidates, recording, 'review-legacy-conflict')).lastrowid
    original = dict(store.conn.execute(
        'SELECT * FROM enrichment_conflict WHERE id=?', (legacy_id,)).fetchone())
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        v080_metadata_evidence.apply(store.conn, Context(4))
        assert metadata_legacy.backfill_conflicts(store.conn) == 1
    rows = list(map(tuple, store.conn.execute(
        'SELECT o.value_kind,o.number_value,o.unit,o.resolution_reason '
        'FROM metadata_conflicts c JOIN metadata_conflict_candidates p ON p.conflict_id=c.id '
        'JOIN metadata_observations o ON o.id=p.observation_id '
        'WHERE c.legacy_record_id=? ORDER BY o.legacy_position', (legacy_id,))))
    assert rows == [
        ('value', 120, 'bpm', None),
        ('value', 120.5, 'bpm', None),
        ('value', 130, 'bpm', None),
        ('unresolved', None, None, 'unsupported_legacy_value'),
        ('unresolved', None, None, 'unsupported_legacy_value'),
    ]
    delivery, = store.conn.execute(
        "SELECT delivery_id FROM metadata_legacy_records WHERE legacy_owner='enrichment_conflict' "
        'AND legacy_id=?', (legacy_id,)).fetchone()
    assert metadata_deliveries.response(store.conn, delivery) == {
        'format': 'legacy-metadata-record-1', 'owner': 'enrichment_conflict', 'row': original}
    before = list(map(tuple, store.conn.execute(
        'SELECT * FROM metadata_observations ORDER BY id')))
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        assert metadata_legacy.backfill_conflicts(store.conn) == 1
    assert list(map(tuple, store.conn.execute(
        'SELECT * FROM metadata_observations ORDER BY id'))) == before
    assert dict(store.conn.execute(
        'SELECT * FROM enrichment_conflict WHERE id=?', (legacy_id,)).fetchone()) == original
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()


def test_native_legacy_history_preserves_original_delivery_provenance(legacy_store):
    store = legacy_store
    entry = store.tracks.upsert_track('review-native-history', 'Song', 'Artist', None, 200)
    recording, key = store.conn.execute(
        'SELECT r.id,r.identity_key FROM catalog_entries e '
        'JOIN recordings r ON r.id=e.recording_id WHERE e.id=?', (entry,)).fetchone()
    provenance = {
        'schema': 1, 'scope': 'artist', 'entity_id': 'historical-artist',
        'kind': 'reported', 'unit': 'provider_tag_count_or_order',
        'delivery_id': 'original-run',
        'match_evidence': {'input_title': 'original input', 'query_kind': 'artist'},
    }
    tags = [{'name': 'Jazz', 'count': 4}]
    with store.conn:
        finding = store.conn.execute(
            'INSERT INTO metadata_findings(entry_id,recording_id,field,provider,value_json,'
            'observed_at,provenance_json) VALUES (?,?,\'tags\',\'native\',?,10,?)',
            (entry, recording, json.dumps(tags), json.dumps(provenance))).lastrowid
    before, = store.enrichment_state.metadata_observations(key)
    assert before['provenance'] == provenance
    original = dict(store.conn.execute(
        'SELECT * FROM metadata_findings WHERE id=?', (finding,)).fetchone())
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        v080_metadata_evidence.apply(store.conn, Context(4))
        metadata_legacy.backfill_observations(store.conn)
    observation, = store.enrichment_state.metadata_observations(key)
    assert observation['provenance'] == provenance
    assert type(observation['delivery_id']) is int
    assert observation['value'] == tags
    assert metadata_native.read(store.conn, observation['id']) == {
        **provenance, 'field': 'tags', 'value': tags}
    assert metadata_deliveries.response(store.conn, observation['delivery_id']) == {
        'format': 'legacy-metadata-record-1', 'owner': 'metadata_findings', 'row': original}
    with store.conn:
        store.conn.execute('BEGIN IMMEDIATE')
        metadata_legacy.backfill_observations(store.conn)
    assert store.enrichment_state.metadata_observations(key) == [observation]
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
