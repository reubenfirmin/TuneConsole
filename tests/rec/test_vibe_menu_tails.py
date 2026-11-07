"""Compact selection retains full reserves and skips unselectable time bands."""
from copy import deepcopy

import pytest

from yt_playlist.core import json_cache
from yt_playlist.rec import home_vibe, vibe_pools, vibe_readiness


def test_filled_comfort_card_does_not_resolve_its_unused_temporal_fallback(
        store, vibe_menus, monkeypatch):
    with store.read_view() as reader:
        value = json_cache.decode(vibe_readiness._source(reader))
        expected = [(preset, [(card.get('pool_lane', card['lane']), [item['key'] for item in card['tracks']])
                              for card in vibe_pools.assemble(reader, 1000, preset)])
                    for preset in home_vibe.PRESETS]
        assert all(cards[-1][0] == 'comfort' for _, cards in expected)
        value['meta']['year_cuts'] = [1990, 2000]
        for preset in home_vibe.PRESETS:
            item = deepcopy(value['tracks'][value['pools'][preset]['comfort'][0]])
            item['key'] = f'unvisited-fallback-{preset}'
            value['pools'][preset]['temporal:0'] = [len(value['tracks'])]
            value['tracks'].append(item)
        calls, original = [], vibe_pools.eligibility_inputs

        def inputs(store, items, now):
            calls.append({item['key'] for item in items})
            return original(store, items, now)

        monkeypatch.setattr(vibe_pools, 'eligibility_inputs', inputs)
        actual = [(preset, [(card['pool_lane'], [item['key'] for item in card['tracks']])
                            for card in cards])
                  for preset, cards in vibe_readiness._menus(
                      reader, value, home_vibe.PRESETS, 1000, 0, (), ())]
        assert actual == expected
        assert len(calls) == 1
        assert not any(key.startswith('unvisited-fallback-') for key in calls[0])


@pytest.mark.parametrize('late_members', ['none', 'all', 'comfort'])
def test_unused_tails_preserve_full_selection_and_expand_when_needed(
        store, vibe_menus, monkeypatch, late_members):
    with store.read_view() as reader:
        value = json_cache.decode(vibe_readiness._source(reader))
        assert value is not None
        if late_members == 'comfort':
            value['meta']['year_cuts'] = [1990, 2000]
            for preset in home_vibe.PRESETS:
                value['pools'][preset]['temporal:0'] = list(value['pools'][preset]['comfort'])
        # Independently use the full stored pools, rather than the compact-index
        # shortcut, to establish the selected lanes and exact track order.
        expected = [(preset, [(card['pool_lane'] if 'pool_lane' in card else card['lane'],
                               [item['key'] for item in card['tracks']])
                              for card in vibe_pools.assemble(reader, 1000, preset)])
                    for preset in home_vibe.PRESETS]
        for preset in home_vibe.PRESETS:
            for lane, members in value['pools'][preset].items():
                for index in range(len(members), vibe_pools.POOL_LIMIT):
                    item = deepcopy(value['tracks'][members[0]])
                    item['key'] = f'unavailable-{preset}-{lane}-{index}'
                    ahead = late_members == 'all' or late_members == lane == 'comfort'
                    item['order'] = {name: [-1e9 if ahead else 1e9, 0, 0]
                                     for name in home_vibe.PRESETS}
                    members.append(len(value['tracks']))
                    value['tracks'].append(item)
        calls, original = [], vibe_pools.eligibility_inputs

        def inputs(store, items, now):
            calls.append({item['key'] for item in items})
            return original(store, items, now)

        monkeypatch.setattr(vibe_pools, 'eligibility_inputs', inputs)
        actual = [(preset, [(card['pool_lane'], [item['key'] for item in card['tracks']])
                            for card in cards])
                  for preset, cards in vibe_readiness._menus(
                      reader, value, home_vibe.PRESETS, 1000, 0, (), ())]
        assert actual == expected
        assert all(len(cards) == 4 and all(len(keys) == 12 for _, keys in cards)
                   for _, cards in actual)
        assert len(calls) == (1 if late_members == 'none' else 2)
        if len(calls) == 2:
            assert calls[0].isdisjoint(calls[1]), 'Expansion must not hydrate the same candidates twice'


@pytest.mark.parametrize('epoch', range(3))
@pytest.mark.parametrize('genre_filter', [False, True])
def test_inactive_time_bands_are_never_resolved(store, vibe_menus, monkeypatch, epoch, genre_filter):
    with store.read_view() as reader:
        family = reader.genres.feature_axes(['Jazz'])['Jazz'][0]
        include = (family,) if genre_filter else ()
        value = json_cache.decode(vibe_readiness._source(reader))
        value['meta'].update(comfort_pool=0, year_cuts=[1990, 2000])
        # The active time band uses a genuine owned reserve. Other bands have
        # distinct sentinels; their presence must neither change output nor
        # cause live catalog/feedback work, including explicit genre requests.
        for preset in home_vibe.PRESETS:
            pools = value['pools'][preset]
            pools[f'temporal:{epoch}'] = list(pools['comfort'])
            for band in range(3):
                if band == epoch:
                    continue
                item = deepcopy(value['tracks'][pools['comfort'][0]])
                item['key'] = f'inactive-{preset}-{band}'
                index = len(value['tracks'])
                value['tracks'].append(item)
                pools[f'temporal:{band}'] = [index]
                value['genres'].setdefault(preset, {})[f'temporal:{band}'] = {family: [index]}
        calls, original = [], vibe_pools.eligibility_inputs

        def inputs(store, items, now):
            calls.append([item['key'] for item in items])
            return original(store, items, now)

        monkeypatch.setattr(vibe_pools, 'eligibility_inputs', inputs)
        actual = vibe_readiness._menus(reader, value, home_vibe.PRESETS, 1000, epoch, include, ())
        assert all(len(cards) == 4 and cards[-1]['pool_lane'] == f'temporal:{epoch}'
                   and all(len(card['tracks']) == 12 for card in cards)
                   for _, cards in actual)
        assert len(calls) == 1
        assert not any(key.startswith('inactive-') for key in calls[0])
        without_inactive = deepcopy(value)
        for preset in home_vibe.PRESETS:
            for band in range(3):
                if band != epoch:
                    without_inactive['pools'][preset].pop(f'temporal:{band}')
                    without_inactive['genres'][preset].pop(f'temporal:{band}')
        assert vibe_readiness._menus(reader, without_inactive, home_vibe.PRESETS, 1000,
                                     epoch, include, ()) == actual
