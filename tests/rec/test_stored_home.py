"""Ordinary Home cards preserve complete opportunities without a bundle read."""
from copy import deepcopy
from tests.derived_rating_support import seed_dislike
import numpy as np
import pytest

from tests.legacy_recommendation_fixture_support import recommendation_predecessor, recommendations
from tests.repos.test_typed_recommendation_publication import publish, plan
from tests.rec.test_stored_fresh import fresh, forbid_legacy
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, mock_snapshot_coordinator,
    native_sql_calls, reuse_reporting_reader, without_query_timing,
)
from yt_playlist.rec import bundle_storage, mode_surfaces, rec_params
from yt_playlist.controllers.recommendation_publications import RecommendationPublications
from yt_playlist.controllers.recommendation_proposals import RecommendationProposals


pytestmark = pytest.mark.usefixtures('native_sql_calls')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Synchronous Home assembly does not construct the web app."""


@pytest.fixture
def home_assembly_reads(fresh, monkeypatch):
    """Compare both assemblers on one actual, independent reporting reader."""
    store, _ = fresh
    with reuse_reporting_reader(store, monkeypatch):
        yield


def modes(store, families):
    store.modes.replace_modes([{'mode_id': index, 'label': f'Mode {index}', 'families': [(family, 1)],
        'centroid': [1., 0.], 'size': 100, 'rep_keys': []} for index, family in families], [], 10)


def candidates(store):
    tracks, after = [], -1
    while True:
        page = store.recommendations.candidate_page('fresh', 'all', 'fresh', after=after, limit=128)
        tracks.extend({key: value for key, value in track.items() if key in bundle_storage.ITEM_FIELDS}
                      for track in page['tracks'])
        after = page['after']
        if page['exhausted']:
            return tracks


def publish_bundle(store, value, *, key='home-test'):
    policy = plan(store).policy_version
    prepared = bundle_storage.prepare(store.conn, value, [], external_key=key,
        policy_version=policy, input_revision=store.prepared_content.serial(), created_at=10)
    return publish(store, prepared).generation_id


def keys(cards):
    return [(card['lane'], card['mode_id'], card['ranker'], [track['key'] for track in card['tracks']])
            for card in cards]


@pytest.fixture
def initial_import_store(tmp_path, monkeypatch, native_sql_calls, without_query_timing):
    # This workflow imports 24 actual catalog entries with no genre metadata.
    # Schema admission, taxonomy curation and snapshot races have other owners.
    with bounded_consumer_schema((), snapshot_guards=False) as schema:
        with _file_consumer_store(tmp_path, schema, snapshot_guards=False) as store:
            with mock_snapshot_coordinator(store, monkeypatch):
                yield store


def test_real_initial_import_prepares_and_serves_home_before_modes_without_legacy_documents(
        initial_import_store, monkeypatch):
    store = initial_import_store
    from tests.home_fixture_support import service
    from yt_playlist.rec import home_prepared, surfaces
    for index in range(24):
        store.tracks.upsert_track(f'import-{index}', f'Song {index}', f'Artist {index}', None, None)
    original = RecommendationProposals._proposal_json
    def read(self, surface):
        assert surface not in ('mode_bundles', 'fresh_pool'), 'Decoded an obsolete candidate bundle'
        return original(self, surface)
    monkeypatch.setattr(RecommendationProposals, '_proposal_json', read)
    monkeypatch.setattr(RecommendationProposals, 'group_pools_json', lambda *args: pytest.fail('Decoded group pools'))
    bundles = mode_surfaces.prepare_bundles(store, 1000)
    assert bundles['all']['explore'] and not store.modes.list_modes(active_only=True)
    def forbidden(*args, **kwargs):
        pytest.fail('Home attempted foreground ranking')
    monkeypatch.setattr(surfaces, 'for_you', forbidden)
    monkeypatch.setattr(surfaces, 'explore_for_you', forbidden)
    prepared = home_prepared.prepare(store, 1001)
    assert home_prepared._valid(prepared)
    result = home_prepared.cards(store, prepared, 1001, service(store), record=False)
    catalog = next(proto for proto in result.protos if proto['lane'] == 'explore')
    assert len(catalog['tracks']) >= 4 and catalog.get('mode_id') is None
    disliked = catalog['tracks'][0]['key']
    seed_dislike(store, disliked, until=None, now=1002)
    refreshed = home_prepared.cards(store, prepared, 1002, service(store), record=False)
    assert [p['tracks'] for p in refreshed.protos] == [p['tracks'] for p in result.protos]


def test_unmodeled_genre_selection_reaches_complete_tail_with_bounded_pages(fresh, monkeypatch):
    from yt_playlist.rec import stored_home
    store, _ = fresh
    with store.conn:
        store.conn.execute("UPDATE recordings SET genre='jazz' WHERE id>=257")
        store.conn.execute("INSERT INTO library_memberships SELECT entry_id,'import',5 FROM discovery_memberships")
    tracks = candidates(store)
    generation = publish_bundle(store, {'all': {'explore': tracks}, '_meta': {'unmodeled': True}})
    original, reads = RecommendationPublications.candidate_page, []
    def page(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        reads.append((kwargs['limit'], len(result['tracks'])))
        return result
    monkeypatch.setattr(RecommendationPublications, 'candidate_page', page)
    original_read = RecommendationProposals._proposal_json
    def read(self, surface):
        assert surface not in ('mode_bundles', 'fresh_pool')
        return original_read(self, surface)
    monkeypatch.setattr(RecommendationProposals, '_proposal_json', read)
    from yt_playlist.rec.home_cards import _carded
    cards = stored_home.unmodeled_cards(store, 10, include=('jazz',), card_builder=_carded)
    assert len(cards) == 1 and cards[0]['lane'] == 'explore'
    assert {track['key'] for track in cards[0]['tracks']} == {f'fresh-{index}' for index in range(254, 260)}
    assert {track['serving_context']['recommendation_generation_id'] for track in cards[0]['tracks']} == {generation}
    assert all(limit == 128 and count <= 128 for limit, count in reads)
    assert not store.conn.execute("SELECT 1 FROM recommendation_generation_refs WHERE kind='pending'").fetchone()


@pytest.mark.parametrize('share', [0, .5, 1])
@pytest.mark.parametrize('steering', [False, True], ids=['neutral', 'penalized'])
def test_real_typed_assembly_matches_complete_legacy_opportunities_and_experiment_evidence(
        fresh, home_assembly_reads, share, steering, monkeypatch):
    store, _ = fresh
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET album='',mb_year='1980'")
    modes(store, [(1, 'rock-classic'), (2, 'rock-classic'), (3, 'rock-classic'), (4, 'rock-classic')])
    rec_params.set_param(store, 'ppr_ab_share', share)
    tracks = candidates(store)
    lanes = ('wheelhouse', 'explore', 'fresh', 'temporal')
    all_pools = {lane: deepcopy(tracks[index * 60:(index + 1) * 60]) for index, lane in enumerate(lanes)}
    value = {'all': all_pools, '_meta': {'full_pools': True, 'comfort_pool': 0,
             'year_cuts': [1990, 2010], 'ppr_policy': {'algorithm': 'controlled-ppr', 'built_at': 10}},
             '_ppr': {str(index + 1): [track['key'] for track in reversed(all_pools[lane])]
                      for index, lane in enumerate(lanes)}}
    for index, lane in enumerate(lanes):
        value[str(index + 1)] = {lane: deepcopy(all_pools[lane][:7])}
    generation = publish_bundle(store, value)
    if steering:
        vectors = [('one', np.array([1., 0.], dtype=np.float32).tobytes())]
        for index in range(260):
            angle = index / 260 * np.pi / 2
            vectors.append((f'fresh-{index}', np.array([np.cos(angle), np.sin(angle)], dtype=np.float32).tobytes()))
        store.rec_model.replace_rec_vectors(vectors)
        seed_dislike(store, 'one', 1000, 10)
    store.rec_proposals.put_proposals('mode_bundles', value, 10)
    store._recommendation_files = False
    try:
        expected = mode_surfaces.assemble_cards(store, 10, 0, mode_order=[1, 2, 3, 4])
    finally:
        store._recommendation_files = True
    selected = []
    original = RecommendationPublications.selected_candidates

    def hydrate(self, surface, selections, **kwargs):
        selected.append(selections)
        return original(self, surface, selections, **kwargs)

    monkeypatch.setattr(RecommendationPublications, 'selected_candidates', hydrate)
    actual = mode_surfaces.assemble_cards(store, 10, 0, mode_order=[1, 2, 3, 4])
    assert len(actual) == 4 and keys(actual) == keys(expected)
    assert len(selected) == sum(1 + bool(card.get('counterfactual')) for card in actual)
    assert all(sum(len(keys) for _, _, keys in batch) == 12 for batch in selected)
    assert any(len(batch) > 1 for batch in selected)  # Selected rows include real backfill.
    for old, new in zip(expected, actual):
        assert new['experiment'] == old['experiment']
        assert [track['key'] for track in new['counterfactual']['tracks']] == [
            track['key'] for track in old['counterfactual']['tracks']]
        assert {track['serving_context']['recommendation_generation_id'] for track in new['tracks']} == {generation}
        assert len(new['tracks']) == 12


def test_thin_credible_comfort_rotates_to_the_current_temporal_band(fresh, monkeypatch):
    store, _ = fresh
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET album='',mb_year='1980'")
    modes(store, [(index, 'rock-classic') for index in range(1, 5)])
    tracks = candidates(store)
    value = {'1': {'wheelhouse': tracks[:30]}, '2': {'explore': tracks[30:60]},
             '3': {'fresh': tracks[60:90]}, '4': {'comfort': tracks[90:93], 'temporal': tracks[90:120]},
             '_meta': {'full_pools': True, 'comfort_pool': 1000, 'year_cuts': [1990, 2010]}}
    publish_bundle(store, value)
    forbid_legacy(monkeypatch)
    cards = mode_surfaces.assemble_cards(store, 10, 0, mode_order=[1, 2, 3, 4])
    assert [(card['lane'], card['mode_id']) for card in cards] == [
        ('wheelhouse', 1), ('explore', 2), ('fresh', 3), ('temporal', 4)]
    assert cards[-1]['label'] == 'Throwback' and len(cards[-1]['tracks']) == 12


def test_rare_tail_and_diversity_overflow_are_reachable_in_bounded_pages(fresh, monkeypatch):
    store, _ = fresh
    with store.conn:
        store.conn.execute("UPDATE recordings SET genre='jazz' WHERE id>=257")
        store.conn.execute("UPDATE youtube_items SET album='' ")
        store.conn.execute("UPDATE youtube_items SET artist='Repeated' WHERE recording_id<257")
    modes(store, [(1, 'jazz')])
    tracks = candidates(store)
    publish_bundle(store, {'1': {'wheelhouse': tracks}, 'all': {'wheelhouse': tracks},
                           '_meta': {'full_pools': True}})
    forbid_legacy(monkeypatch)
    reads, original = [], RecommendationPublications.candidate_page
    def page(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        reads.append((kwargs['limit'], len(result['tracks'])))
        return result
    monkeypatch.setattr(RecommendationPublications, 'candidate_page', page)
    cards = mode_surfaces.assemble_cards(store, 10, 0, include_genres=('jazz',), mode_order=[1])
    assert keys(cards) == [('wheelhouse', 1, 'cosine', [f'fresh-{index}' for index in range(254, 260)])]
    assert len(reads) >= 3 and all(limit <= 128 and count <= limit for limit, count in reads)


def test_first_unavailable_page_and_current_eligibility_do_not_hide_the_remaining_pool(fresh, monkeypatch):
    store, _ = fresh
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET album=''")
    modes(store, [(1, 'rock-classic')])
    tracks = candidates(store)
    publish_bundle(store, {'1': {'wheelhouse': tracks}, '_meta': {'full_pools': True}})
    with store.conn:
        store.conn.execute('UPDATE youtube_items SET available=0 WHERE recording_id<131')
        store.conn.execute("UPDATE youtube_items SET title='corrected ' || title,duration_s=241 "
                           'WHERE recording_id>=131')
        store.conn.execute('UPDATE recordings SET bpm=123 WHERE id>=131')
    seed_dislike(store, 'fresh-128', 100, 10)
    forbid_legacy(monkeypatch)
    cards = mode_surfaces.assemble_cards(store, 10, 0, mode_order=[1])
    selected = [int(track['key'].removeprefix('fresh-')) for track in cards[0]['tracks']]
    assert len(selected) == 12 and len(set(selected)) == 12
    assert all(129 <= index < 260 for index in selected)
    assert all(track['title'] == f"corrected {track['key']} title"
               and track['duration'] == 241 and track['bpm'] == 123 for track in cards[0]['tracks'])


def test_whole_considered_bucket_is_excluded_from_later_card_backfill(fresh, monkeypatch):
    store, _ = fresh
    with store.conn:
        store.conn.execute("UPDATE youtube_items SET album=''")
    modes(store, [(1, 'rock-classic'), (2, 'rock-classic')])
    tracks = candidates(store)
    publish_bundle(store, {'1': {'wheelhouse': tracks[:128]}, '2': {'explore': tracks[:4]},
                           'all': {'explore': tracks}, '_meta': {'full_pools': True}})
    forbid_legacy(monkeypatch)
    cards = mode_surfaces.assemble_cards(store, 10, 0, mode_order=[1, 2])
    assert [(card['lane'], card['mode_id']) for card in cards] == [('wheelhouse', 1), ('explore', 2)]
    assert [track['key'] for track in cards[1]['tracks']] == [f'fresh-{index}' for index in range(128, 140)]


def test_failed_hydration_releases_reader_retention_and_private_scratch_files(fresh, monkeypatch):
    store, _ = fresh
    modes(store, [(1, 'rock-classic')])
    publish_bundle(store, {'1': {'wheelhouse': candidates(store)}, '_meta': {'full_pools': True}})
    def fail(self, *args, **kwargs):
        raise RuntimeError('controlled hydration failure')
    monkeypatch.setattr(RecommendationPublications, 'selected_candidates', fail)
    rec_params.set_param(store, 'modes_album_cap', 12)
    with pytest.raises(RuntimeError, match='controlled hydration'):
        mode_surfaces.assemble_cards(store, 10, 0, mode_order=[1])
    assert not store.conn.execute("SELECT 1 FROM group_evidence_owners WHERE kind='pending'").fetchone()


def test_unprepared_user_needs_no_legacy_bundle(recommendations, monkeypatch):
    forbid_legacy(monkeypatch)
    assert mode_surfaces.assemble_cards(recommendations, 10, 0) == []
