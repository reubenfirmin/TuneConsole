"""Malformed native evidence must not replace a captured result or stop the waterfall."""

# These fixtures deliberately stage v80 on the isolated v69 + v74/v75
# predecessor, matching test_metadata_attempt_fields rather than changing the
# normal production migration registry.
from tests.core.test_metadata_evidence_migration import additive_catalog, catalog
from tests.test_waterfall import FakeProv, _cfg
from yt_playlist.core import metadata_deliveries
from yt_playlist.providers.base import EnrichmentResult, MetadataObservation
from yt_playlist.providers.waterfall import run_waterfall


def test_invalid_native_answer_keeps_original_result_and_allows_healthy_provider(catalog):
    store = catalog
    entry = store.tracks.upsert_track(
        'waterfall-recovery', 'Song', 'Artist', None, 200,
        thumbnail='https://example.test/cover.jpg')
    track = dict(store.tracks.track_for_waterfall(entry))
    original_fields = {'genre': 'Jazz'}

    class InvalidNativeProvider(FakeProv):
        def probe(self, track, store):
            result = super().probe(track, store)
            # The result fields and observation are JSON-serializable. The
            # native owner rejects this unit after the result was captured.
            observation = MetadataObservation(
                'tags', [{'name': 'Jazz', 'count': 4}], 'artist',
                'review-artist', 'reported', unit=42)
            return EnrichmentResult(self.name, result.fields, (observation,))

    broken = InvalidNativeProvider('broken', {'Song': original_fields})
    healthy = FakeProv('healthy', {'Song': {'genre': 'Rock'}})
    events = []
    completed = None
    failure = None
    try:
        completed = run_waterfall(
            store, [track], _cfg(('broken', True), ('healthy', True)), events.append,
            registry={'broken': broken, 'healthy': healthy},
            run_id='review-native-recovery', now_fn=lambda: 100)
    except Exception as exc:
        # Check the committed original even if retry admission aborts the run.
        # The assertion below still requires successful waterfall recovery.
        failure = exc

    originals = store.conn.execute(
        'SELECT id,raw_filename,raw_digest,raw_byte_size FROM metadata_deliveries '
        "WHERE provider='broken' AND delivery_key='enrichment-result:review-native-recovery'").fetchall()
    assert len(originals) == 1
    original = originals[0]
    assert original['raw_filename'] and original['raw_digest'] and original['raw_byte_size'] > 0
    # This public reader verifies the retained bytes against their stored digest.
    assert metadata_deliveries.response(store.conn, original['id']) == {
        'format': 'enrichment-result-1', 'fields': original_fields}
    assert not store.conn.execute(
        'SELECT 1 FROM native_metadata_observations o '
        "JOIN metadata_deliveries d ON d.id=o.delivery_id WHERE d.provider='broken'").fetchone()

    assert failure is None, f'Native validation recovery aborted the waterfall: {failure!r}'
    assert broken.calls == healthy.calls == ['Song']
    state = store.enrichment_state.enrichment_provider_state(track['recording_id'])
    assert state['broken']['status'] == 'retry'
    assert state['broken']['failures'] == 1
    assert state['broken']['retry_at'] > 100
    assert state['broken']['error']
    assert state['healthy']['status'] == 'found'
    assert state['healthy']['fields']['genre'] == 'Rock'
    assert list(map(tuple, store.conn.execute(
        'SELECT provider,status FROM provider_attempts ORDER BY provider'))) == [
            ('broken', 'retry'), ('healthy', 'found')]
    assert store.tracks.get_track_enrichment(entry)[0] == 'Rock'
    assert completed == []
    track_events = [event for event in events if event['type'] == 'track']
    assert len(track_events) == 1 and track_events[0]['fully_attempted'] is False
    assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
