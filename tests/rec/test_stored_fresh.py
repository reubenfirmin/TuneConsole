"""Fresh selection uses complete published ranked membership."""
from dataclasses import replace

import pytest

from tests.legacy_recommendation_fixture_support import recommendations
from tests.enrichment_support import seed_discovered_enrichment
from tests.store_fixture_support import native_sql_calls
from tests.repos.test_typed_recommendation_publication import publish, plan
from yt_playlist.core import recommendation_outputs as outputs, recommendation_pools as pools
from yt_playlist.rec import fresh_pool
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals
from yt_playlist.controllers.recommendation_publications import RecommendationPublications
from yt_playlist.repos import recommendation_publication as publication


pytestmark = pytest.mark.usefixtures('native_sql_calls')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Synchronous recommendation readers do not construct the web app."""


@pytest.fixture
def fresh(recommendations):
    store = recommendations
    records = [(index + 3, f'fresh-{index}', 'jazz' if index >= 258 else 'rock')
               for index in range(260)]
    items = [(f'fresh-upload-{index}', recording, key + ' title', f'artist-{index}', 'album')
             for index, (recording, key, _genre) in enumerate(records)]
    with store.conn:
        store.conn.executemany('INSERT INTO recordings(id,identity_key) VALUES (?,?)',
                               [(recording, key) for recording, key, _ in records])
        store.conn.executemany('INSERT INTO track_sources(video_id,recording_id,title,artist,album) '
                               'VALUES (?,?,?,?,?)', items)
        store.conn.execute('UPDATE recordings SET discovery_entry_id=t.id,discovery_found_at=1 '
                           'FROM track_sources t WHERE recordings.id=t.recording_id AND recordings.id BETWEEN 3 AND 262')
    for _recording, key, genre in records:
        seed_discovered_enrichment(store, key, genre, None)
    candidates = [pools.Candidate(recording, 1 / (index + 1))
                  for index, (recording, _key, _genre) in enumerate(records)]
    original = plan(store)
    ranked = pools.prepare('fresh', original.policy_version, 'c' * 64, 'fresh', candidates)
    prepared = replace(original, slots=(publication.Slot('fresh', 'all', 'fresh', ranked),),
        outputs=(publication.Output('fresh', 'empty', outputs.encode('fresh', 'empty', {})),))
    generation = publish(store, prepared).generation_id
    return store, generation


def forbid_legacy(monkeypatch):
    original = RecommendationProposals._proposal_json

    def forbidden(self, surface, *args, **kwargs):
        if surface in ('mode_bundles', 'fresh_pool'):
            pytest.fail('Typed Fresh serving attempted to decode a legacy candidate proposal')
        return original(self, surface, *args, **kwargs)
    monkeypatch.setattr(RecommendationProposals, '_proposal_json', forbidden)


def test_rare_family_after_the_global_head_remains_reachable_in_one_rank_read(fresh, monkeypatch):
    store, generation = fresh
    forbid_legacy(monkeypatch)
    reads, original = [], RecommendationPublications.complete_ranks

    def ranks(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        reads.append(len(result))
        return result
    monkeypatch.setattr(RecommendationPublications, 'complete_ranks', ranks)
    tracks = fresh_pool.pick(store, 10, include_genres=('jazz',), size=2, minimum=2)
    assert [track['key'] for track in tracks] == ['fresh-258', 'fresh-259']
    assert reads == [260]
    assert {track['serving_context']['recommendation_generation_id'] for track in tracks} == {generation}


def test_fresh_round_robin_preserves_requested_families(fresh, monkeypatch):
    store, _ = fresh
    forbid_legacy(monkeypatch)
    tracks = fresh_pool.pick(store, 10, include_genres=('rock-classic', 'jazz'), size=4)
    assert [track['genre'] for track in tracks] == ['Rock', 'Jazz', 'Rock', 'Jazz']
    assert len({track['key'] for track in tracks}) == 4
    assert fresh_pool.pick(store, 10, include_genres=('rock-classic', 'ambient'), size=4) == []


def test_small_unfiltered_preview_preserves_source_order(fresh, monkeypatch):
    store, _ = fresh
    forbid_legacy(monkeypatch)
    monkeypatch.setattr(RecommendationPublications, 'candidate_page',
                        lambda *args, **kwargs: pytest.fail('Fresh rank pagination'))
    tracks = fresh_pool.pick(store, 10, size=4)
    assert [track['key'] for track in tracks] == [f'fresh-{index}' for index in range(4)]


def test_live_membership_and_availability_changes_do_not_hide_later_fresh_candidates(fresh, monkeypatch):
    store, _ = fresh
    forbid_legacy(monkeypatch)
    with store.conn:
        entry = store.conn.execute('SELECT discovery_entry_id FROM recordings WHERE id=3').fetchone()[0]
        store.conn.execute("UPDATE track_sources SET library_origin='import',library_added_at=5 WHERE id=? AND library_origin IS NULL", (entry,))
        store.conn.execute("UPDATE track_sources SET available=0 WHERE video_id='fresh-upload-1'")
    seed_discovered_enrichment(store, 'fresh-2', 'jazz', None)
    tracks = fresh_pool.pick(store, 10, include_genres=('rock-classic',), size=2, minimum=2)
    assert [track['key'] for track in tracks] == ['fresh-3', 'fresh-4']


def test_unprepared_user_has_no_dependency_on_a_legacy_bundle(recommendations, monkeypatch):
    forbid_legacy(monkeypatch)
    assert fresh_pool.pick(recommendations, 10, size=4) == []
