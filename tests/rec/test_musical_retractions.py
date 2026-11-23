"""Source association retractions rebuild profiles without altering source history."""
import json

import pytest

from yt_playlist.providers.base import MetadataObservation
from yt_playlist.rec import musical_preparation, vibe_preparation
from yt_playlist.rec.progressive import Preparation
from tests.musical_support import prepare_profiles
from yt_playlist.rec import musical_profiles


def deliver(store, entry, entities, run):
    store.enrichment_state.record_metadata_observations(entry, 'fixture-retraction', [
        MetadataObservation('tags', [{'name': 'Ambient' if entity == 'a' else 'Metal'}],
            'artist', entity, 'reported', match_evidence={'method': 'title_artist_match'})
        for entity in entities], run_id=run, now=10)


def settled(store):
    preparation = Preparation(store)
    for _ in range(32):
        prepare_profiles(store)
        if not vibe_preparation.due(store):
            break
        preparation.capture(1000)
    else:
        pytest.fail('Independent vibe preparation did not settle')
    assert not store.prepared_content.has_pending()
    assert not musical_preparation.due(store) and not vibe_preparation.due(store)


def source_rows(store):
    return {row['entity_id']: dict(row) | {'selected_observations': json.loads(row['selected_observations'])}
            for row in store.conn.execute(
        'SELECT *,musical_profile_digest AS digest FROM rec_musical_sources '
        'WHERE musical_profile_version IS NOT NULL ORDER BY source_key')}


def versions(store):
    return store.musical_profiles.by_keys(['song|artist'])['song|artist']['source_versions']


def test_profiles_drain_after_a_bounded_capture_defers(store, monkeypatch):
    entry = store.tracks.upsert_track('video', 'Song', 'Artist', '', 200)
    deliver(store, entry, ('a',), 'initial')
    original = Preparation.capture
    calls = []

    def defer_once(self, now):
        calls.append(now)
        return False if len(calls) == 1 else original(self, now)

    monkeypatch.setattr(Preparation, 'capture', defer_once)
    settled(store)
    assert len(calls) > 1
    assert set(versions(store)) == {source_rows(store)['a']['digest']}


@pytest.mark.parametrize(('initial', 'corrected'), [(('a', 'b'), ('a',)), (('a',), ('b',))])
def test_delivery_contraction_and_replacement_rebuild_from_current_links(store, initial, corrected):
    entry = store.tracks.upsert_track('video', 'Song', 'Artist', '', 200)
    deliver(store, entry, initial, 'initial')
    settled(store)
    before, counters = source_rows(store), store.prepared_content.counters()
    historical = store.enrichment_state.metadata_observations('song|artist')
    assert set(versions(store)) == {row['digest'] for row in before.values()}
    deliver(store, entry, corrected, 'corrected')
    assert [row[0] for row in store.prepared_content.pending(32)] == ['song|artist']
    observations = store.enrichment_state.metadata_observations('song|artist')
    assert len(observations) == len(initial) + len(corrected)
    assert observations[:len(initial)] == historical
    replacement = observations[-1]
    assert replacement['field'] == 'tags'
    assert replacement['provenance']['delivery_id'] == 'corrected'
    assert replacement['provenance']['entity_id'] == corrected[0]
    expected = dict(before)
    if len(initial) == 2:
        assert before['a']['selected_observations']['tags'] != replacement['id']
        expected['a'] = before['a'] | {'selected_observations': {'tags': replacement['id']}}
        assert source_rows(store) == expected
        assert store.musical_profiles.pending() is None
        # Removing a source link invalidates the joint model's masked metadata
        # inputs immediately, even though no source profile needs rebuilding.
        from yt_playlist.rec import vibe_joint_preparation
        assert store.vibe_predictions.pending() is None
        assert vibe_preparation.due(store) == vibe_joint_preparation.due(store)
    settled(store)
    current = source_rows(store)
    assert set(versions(store)) == {current[entity]['digest'] for entity in corrected}
    assert current[corrected[0]]['selected_observations'] == {'tags': replacement['id']}
    assert all(current[entity] == row for entity, row in expected.items())
    assert store.prepared_content.counters() == counters
    assert store.enrichment_state.metadata_observations('song|artist') == observations
