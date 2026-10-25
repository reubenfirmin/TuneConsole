"""Shared snapshot inputs must let bounded Home preparation make useful progress."""
import pytest

from tests.rec.test_home_prepared import seed
from yt_playlist.rec import (discover, embedding_vectors as embed, home_prepared, home_themes, home_vibe,
                             mode_surfaces, rec_params, surfaces, transient)


def test_each_preparation_snapshot_reuses_listening_signals_for_all_discovery_families(store, monkeypatch):
    seed(store)
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
def test_snapshot_reuses_eligibility_and_union_metadata_without_changing_offers(store, monkeypatch, include):
    bundles = seed(store)
    bundles['_ppr'] = {str(mid): [item['key'] for items in bundles[str(mid)].values()
                                  for item in reversed(items)] for mid in range(6)}
    store.rec_proposals.put_proposals('mode_bundles', bundles, 1)
    rec_params.set_param(store, 'ppr_ab_share', 0.5)
    store.rec_model.record_dislike(bundles['0']['wheelhouse'][0]['key'], until=None, now=1)
    orders = [(epoch, tuple(range(epoch, 6)) + tuple(range(epoch))) for epoch in range(3)]
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        expected = [mode_surfaces.assemble_cards(reader, 1000, epoch, mode_order=order,
                                                include_genres=include) for epoch, order in orders]
        preparation = {'leans': transient.facet_leans(reader, 1000),
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
        actual = [mode_surfaces.assemble_cards(reader, 1000, epoch, mode_order=order,
                                              include_genres=include, preparation=preparation)
                  for epoch, order in orders]
        assert actual == expected
        assert all(cards for cards in actual)
        assert any(card.get('counterfactual') for cards in actual for card in cards)
        assert len(checks) == 1
        assert preparation['assembly']['full_pools']
        for lane in ('wheelhouse', 'explore', 'fresh'):
            union = frozenset(item['key'] for item in bundles['all'][lane])
            assert resolved.count(union) == int(lane in preparation['assembly']['full_pools'])


@pytest.mark.parametrize('missing_bundles', [False, True])
def test_preparation_never_rebuilds_the_library_for_cold_or_shallow_bundles(store, monkeypatch,
                                                                        missing_bundles):
    bundles = seed(store)
    bundles['_meta']['full_pools'] = False
    store.rec_proposals.put_proposals('mode_bundles', None if missing_bundles else bundles, 1)

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
