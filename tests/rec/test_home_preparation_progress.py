"""Shared snapshot inputs must let bounded Home preparation make useful progress."""
from collections import Counter
from tests.derived_rating_support import seed_dislike

import pytest

from tests.recommendation_support import publish_bundles
from tests.recommendation_support import seed_bundle_subjects
from tests.home_prepared_support import publication_inputs
from tests.rec.test_home_prepared import (cached_menu_schema,
                                         menu_publication_store as store,
                                         one_menu_per_attempt)
from yt_playlist.rec import (discover, embedding_vectors as embed, home_prepared, home_themes, home_vibe,
                             mode_surfaces, rec_params, stored_home, surfaces, transient)


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Preparation snapshots do not start the application web workers."""


@pytest.fixture(autouse=True)
def independent_history_summaries(monkeypatch):
    # History summary preparation has its own checkpoint/recovery owners in
    # test_maintenance_logging. These cases inspect discovery and mix inputs.
    monkeypatch.setattr(home_prepared, 'prepare_revisit', lambda *args: None)


def candidate_inputs(store, *, genres=('Jazz', 'Techno'), lanes=mode_surfaces.CARD_SURFACES):
    """Actual genres with a shallow candidate page, without fitted models.

    These cases own snapshot reuse and the refusal to rebuild source models.
    Four candidates per genre/lane retain real selection, PPR counterfactuals,
    suppression and metadata resolution without 192 prepared profiles.
    """
    from tests.test_home_other_genres import genre_bundles

    bundles = genre_bundles(store, genres=genres, tracks_per_lane=4)
    for mode, pools in bundles.items():
        if not mode.startswith('_'):
            for lane in list(pools):
                if lane not in lanes:
                    del pools[lane]
    with store._lock, store.conn.atomic():
        seed_bundle_subjects(store, bundles)
    return bundles


@pytest.fixture
def eligibility_inputs(store):
    # A cold consumer needs this small publication once. Construct it directly
    # in its private Store instead of constructing, inventorying and cloning a
    # second SQL/file library before the same selection check.
    bundles = candidate_inputs(store, lanes=('wheelhouse',))
    bundles['_ppr'] = {str(mid): [item['key'] for items in bundles[str(mid)].values()
                                 for item in reversed(items)] for mid in range(2)}
    publish_bundles(store, bundles, 1)
    return bundles


def test_each_preparation_snapshot_reuses_listening_signals_for_all_discovery_families(
        store, monkeypatch, one_menu_per_attempt):
    # Candidate ranking is below this snapshot-sharing check. Retain real
    # discovery selection and publication without preparing 192 profiles and
    # twelve independently ranked menus first.
    publication_inputs(store, monkeypatch)
    for index, genre in enumerate(('House', 'Techno', 'Jazz', 'Ambient', 'Trance', 'Classical')):
        store.discovery.upsert_discovered_artist(f'New {index}', 0.5, [], [], None, 1, genre=genre)
        store.discovery.upsert_discovered_album(f'new-{index}', f'New {index}', 'New album',
                                                '2026', None, 1, genre=genre)
    calls, original = [], transient.facet_leans

    def counted(reader, now):
        calls.append(reader)
        return original(reader, now)

    monkeypatch.setattr(transient, 'facet_leans', counted)
    payload = home_prepared.prepare(store, 1000)
    assert payload is not None
    assert len(payload['discovery_artists']) == len(payload['discovery_albums']) == 6
    # Discovery publishes independently before mix assembly, so it owns its own
    # bounded snapshot. Both discovery families still share that snapshot's signals.
    assert len(calls) == 2
    assert calls[0].conn is not calls[1].conn
    assert all(reader.conn is not store.conn for reader in calls)


@pytest.mark.parametrize('leans', [{}, {'genre:house': 0.8, 'genre:ambient': -0.7}])
def test_shared_discovery_signals_preserve_selection_and_mutes(store, monkeypatch, leans):
    for index, genre in enumerate(('House', 'Techno', 'Jazz', 'Ambient', 'Trance', '')):
        store.discovery.upsert_discovered_artist(f'New {index}', 0.5, [], [], None, 1, genre=genre)
        store.discovery.upsert_discovered_album(f'new-{index}', f'New {index}', f'New album {index}',
                                                '2026' if index % 2 else '2000', None, 1, genre=genre)
    store.rec_model.set_weight('genre:techno', 0, lo=0)
    store.rec_model.set_weight('genre:house', 1.4, now=1000)
    store.rec_model.set_lean('genre:ambient', 1.5, now=1000)
    monkeypatch.setattr(transient, 'facet_leans', lambda *args: leans)
    expected_albums = discover.pick_discovered_albums(store, 5, 1000)
    expected_artists = discover.pick_discovered_artists(store, 5, 1000)

    def forbidden(*args):
        raise AssertionError('The snapshot already captured listening signals')

    monkeypatch.setattr(transient, 'facet_leans', forbidden)
    assert discover.pick_discovered_albums(store, 5, 1000, facet_leans=leans) == expected_albums
    assert discover.pick_discovered_artists(store, 5, 1000, facet_leans=leans) == expected_artists
    assert all(item['genre'] != 'Techno' for item in expected_albums + expected_artists)


@pytest.mark.parametrize('include', [(), ('jazz', 'techno')])
def test_snapshot_reuses_eligibility_and_union_metadata_without_changing_offers(
        store, monkeypatch, include, eligibility_inputs):
    # A single lane still overlaps its mode and general pages across three
    # menus. Other lane/ranker selection contracts stay in test_stored_home.
    bundles = eligibility_inputs
    rec_params.set_param(store, 'ppr_ab_share', 0.5)
    seed_dislike(store, bundles['0']['wheelhouse'][0]['key'], until=None, now=1)
    orders = [(epoch, (epoch % 2, (epoch + 1) % 2)) for epoch in range(3)]
    with store.read_snapshot() as snapshot, stored_home.preparation_inputs() as metadata_inputs:
        reader = snapshot.store
        # Catalog hydration is below the eligibility/metadata-reuse boundary.
        # Resolve the real private fixture once; replay those immutable facts
        # while retaining actual pool paging, selection, PPR counterfactuals,
        # dislikes and genre resolution across all six assemblies. Hydration's
        # current-catalog contract remains in test_recommendation_publication.
        keys = [track['key'] for tracks in bundles['all'].values() for track in tracks]
        facts = reader.recommendations._candidate_facts(keys)
        monkeypatch.setattr(reader.recommendations, '_candidate_facts',
                            lambda requested: {key: facts[key] for key in requested})
        expected = [mode_surfaces.assemble_cards(reader, 1000, epoch, mode_order=order,
                                                include_genres=include) for epoch, order in orders]
        preparation = {'leans': transient.facet_leans(reader, 1000),
                       'metadata_inputs': metadata_inputs,
                       'vibe_rank': home_vibe.ranker(reader, 1000)}
        checks, resolved = [], []
        check = reader.rec_eligibility.suppressed_candidate_keys
        resolve = home_themes.resolved_genres

        def counted_check(keys, now):
            checks.append(set(keys))
            return check(keys, now)

        def counted_resolve(store, items):
            resolved.append(frozenset(item['key'] for item in items))
            return resolve(store, items)

        monkeypatch.setattr(reader.rec_eligibility, 'suppressed_candidate_keys', counted_check)
        monkeypatch.setattr(home_themes, 'resolved_genres', counted_resolve)
        actual = []
        for epoch, order in orders:
            actual.append(mode_surfaces.assemble_cards(reader, 1000, epoch, mode_order=order,
                          include_genres=include, preparation=preparation))
        assert actual == expected
        assert all(cards for cards in actual)
        assert any(card.get('counterfactual') for cards in actual for card in cards)
        # Typed owners seek bounded pages instead of inflating one union. Each
        # recording is still checked/resolved only once across all three menus.
        assert checks and resolved
        assert all(len(keys) <= 128 for keys in checks + resolved)
        checked = Counter(key for keys in checks for key in keys)
        metadata = Counter(key for keys in resolved for key in keys)
        assert checked == metadata
        assert set(checked.values()) == {1}
        assert store.rec_model.disliked_identity_keys() <= checked.keys()


@pytest.mark.parametrize('missing_bundles', [False, True])
def test_preparation_never_rebuilds_the_library_for_cold_or_shallow_bundles(
        store, monkeypatch, one_menu_per_attempt, missing_bundles):
    # One genre still fills each lane and proves preparation can consume a
    # shallow publication. Cross-genre/menu reuse is checked separately above.
    bundles = candidate_inputs(store, genres=('Jazz',))
    bundles['_meta']['full_pools'] = False
    if not missing_bundles:
        publish_bundles(store, bundles, 1)

    def forbidden(*args, **kwargs):
        raise AssertionError('Preparation must wait for refreshed bundles instead of reranking the library')

    monkeypatch.setattr(embed, 'load_content_vectors', forbidden)
    monkeypatch.setattr(surfaces, 'for_you', forbidden)
    monkeypatch.setattr(surfaces, 'explore_for_you', forbidden)
    payload = home_prepared.prepare(store, 1000)
    if missing_bundles:
        assert payload is None
    else:
        assert home_prepared._valid(payload)
        assert all(menu['protos'] for menu in payload['menus'])
