"""Fresh selection traverses complete typed membership with bounded pages."""
from dataclasses import replace

import pytest

from tests.core.test_compact_groups_migration import predecessor
from tests.repos.test_compact_group_publication import database
from tests.repos.test_typed_recommendation_publication import recommendations, publish, plan
from yt_playlist.core import recommendation_outputs as outputs, recommendation_pools as pools
from yt_playlist.rec import fresh_pool
from yt_playlist.repos.rec_proposals import RecProposalsRepo
from yt_playlist.repos.recommendation_publications import RecommendationPublicationsRepo
from yt_playlist.repos import recommendation_publication as publication


@pytest.fixture
def fresh(recommendations):
    store = recommendations
    candidates = []
    with store.conn:
        for index in range(260):
            recording = index + 3
            key, video = f'fresh-{index}', f'fresh-upload-{index}'
            genre = 'jazz' if index >= 258 else 'rock'
            store.conn.execute('INSERT INTO recordings(id,identity_key,genre) VALUES (?,?,?)',
                               (recording, key, genre))
            store.conn.execute('INSERT INTO youtube_items(video_id,recording_id,title,artist,album) VALUES (?,?,?,?,?)',
                               (video, recording, key + ' title', f'artist-{index}', 'album'))
            entry = store.conn.execute('INSERT INTO catalog_entries(recording_id,video_id) VALUES (?,?) RETURNING id',
                                       (recording, video)).fetchone()[0]
            store.conn.execute('INSERT INTO discovery_memberships(recording_id,entry_id,found_at) VALUES (?,?,?)',
                               (recording, entry, 1))
            candidates.append(pools.Candidate(recording, 1 / (index + 1)))
    original = plan(store)
    ranked = pools.prepare('fresh', original.policy_version, None, 'c' * 64, 'fresh', candidates)
    prepared = replace(original, slots=(publication.Slot('fresh', 'all', 'fresh', ranked),),
        outputs=(publication.Output('fresh', 'empty', outputs.encode('fresh', 'empty', {})),))
    generation = publish(store, prepared).generation_id
    return store, generation


def forbid_legacy(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Typed Fresh serving attempted to decode a legacy proposal')
    monkeypatch.setattr(RecProposalsRepo, '_proposal_json', forbidden)


def test_rare_family_after_the_global_head_remains_reachable_in_bounded_reads(fresh, monkeypatch):
    store, generation = fresh
    forbid_legacy(monkeypatch)
    pages, original = [], RecommendationPublicationsRepo.candidate_page

    def page(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        pages.append((kwargs['limit'], len(result['tracks'])))
        return result
    monkeypatch.setattr(RecommendationPublicationsRepo, 'candidate_page', page)
    tracks = fresh_pool.pick(store, 10, include_genres=('jazz',), size=2, minimum=2)
    assert [track['key'] for track in tracks] == ['fresh-258', 'fresh-259']
    assert len(pages) >= 3 and all(limit == 128 and count <= 128 for limit, count in pages)
    assert {track['serving_context']['recommendation_generation_id'] for track in tracks} == {generation}


def test_fresh_round_robin_preserves_requested_families(fresh, monkeypatch):
    store, _ = fresh
    forbid_legacy(monkeypatch)
    tracks = fresh_pool.pick(store, 10, include_genres=('rock-classic', 'jazz'), size=4)
    assert [track['genre'] for track in tracks] == ['rock', 'jazz', 'rock', 'jazz']
    assert len({track['key'] for track in tracks}) == 4
    assert fresh_pool.pick(store, 10, include_genres=('rock-classic', 'ambient'), size=4) == []


def test_small_unfiltered_preview_stops_after_one_page(fresh, monkeypatch):
    store, _ = fresh
    forbid_legacy(monkeypatch)
    reads, original = [], RecommendationPublicationsRepo.candidate_page

    def page(self, *args, **kwargs):
        reads.append(kwargs['after'])
        return original(self, *args, **kwargs)
    monkeypatch.setattr(RecommendationPublicationsRepo, 'candidate_page', page)
    tracks = fresh_pool.pick(store, 10, size=4)
    assert [track['key'] for track in tracks] == [f'fresh-{index}' for index in range(4)]
    assert reads == [-1]


def test_live_membership_and_availability_changes_do_not_hide_later_fresh_candidates(fresh, monkeypatch):
    store, _ = fresh
    forbid_legacy(monkeypatch)
    with store.conn:
        entry = store.conn.execute('SELECT entry_id FROM discovery_memberships WHERE recording_id=3').fetchone()[0]
        store.conn.execute("INSERT INTO library_memberships VALUES (?,'import',5)", (entry,))
        store.conn.execute("UPDATE youtube_items SET available=0 WHERE video_id='fresh-upload-1'")
        store.conn.execute("UPDATE recordings SET genre='jazz' WHERE id=5")
    tracks = fresh_pool.pick(store, 10, include_genres=('rock-classic',), size=2, minimum=2)
    assert [track['key'] for track in tracks] == ['fresh-3', 'fresh-4']


def test_unprepared_user_has_no_dependency_on_a_legacy_bundle(recommendations, monkeypatch):
    forbid_legacy(monkeypatch)
    assert fresh_pool.pick(recommendations, 10, size=4) == []
