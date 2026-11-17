"""Collaborations share performers; scarcity must not make a playlist short."""
from collections import Counter
from types import SimpleNamespace

import numpy as np
import pytest

from yt_playlist.rec import artist_variety, journeys, mode_rules


def song(key, artist_ids, *, year=2020):
    return {'key': str(key), 'artist': f'Credit {key}', 'artist_ids': artist_ids,
            'album': '', 'genre': '', 'decade': year}


def test_collaborations_share_the_cap_before_ranked_alternatives():
    pool = [song(0, ['rita', 'elisabeth']), song(1, ['rita', 'lau']),
            song(2, ['rita', 'toni']), song(3, ['rita', 'elisabeth'])]
    pool += [song(i, [str(i)]) for i in range(4, 20)]
    selected = mode_rules._diversify(pool, 2, 2, limit=12)
    assert len(selected) == 12
    assert sum('rita' in item['artist_ids'] for item in selected) == 2
    assert [item['key'] for item in selected[:2]] == ['0', '1']


@pytest.mark.parametrize('available', [4, 12, 20])
def test_soft_caps_fill_a_scarce_pool_without_duplicating_recordings(available):
    pool = [song(i, ['rita', 'elisabeth']) for i in range(available)]
    selected = artist_variety.select([*pool, *pool], 2, 1, limit=12)
    assert len(selected) == min(12, available)
    assert len({row['key'] for row in selected}) == len(selected)


def test_artist_labels_are_never_split_and_homonyms_with_ids_remain_distinct():
    assert artist_variety.members({'artist': 'Earth, Wind & Fire'}) == {'credit:earth, wind & fire'}
    tracks = [{**song(i, [str(i)]), 'artist': 'Same name'} for i in range(12)]
    assert len(artist_variety.select(tracks, 1, 1, limit=12)) == 12


def test_second_credit_counts_even_when_primary_artists_differ():
    pool = [song(i, [str(i), 'shared']) for i in range(4)]
    pool += [song(i, [str(i)]) for i in range(4, 20)]
    selected = artist_variety.select(pool, 2, 2, limit=12)
    assert sum('shared' in row['artist_ids'] for row in selected) == 2


@pytest.mark.parametrize('journey', journeys.JOURNEYS)
def test_journeys_space_shared_performers_without_changing_membership(journey):
    tracks = [song(i, ['rita', f'guest-{i}']) for i in range(4)]
    tracks += [song(i, [str(i)]) for i in range(4, 12)]
    feat = lambda row: {**row, 'energy': .5, 'plays': 0, 'recency': 0}
    ordered = journeys.journey_order(tracks, journey, 17, feat)
    assert Counter(row['key'] for row in ordered) == Counter(row['key'] for row in tracks)
    assert not any(artist_variety.members(a) & artist_variety.members(b) for a, b in zip(ordered, ordered[1:]))
    assert ordered == journeys.journey_order(tracks, journey, 17, feat)


def test_axis_band_seam_uses_all_members_and_searches_past_the_second_track():
    tracks = [song(i, ['rita'] if i < 2 else [str(i)], year=2000) for i in range(4)]
    tracks += [song(i, ['rita'] if i < 7 else ['other'], year=2020) for i in range(4, 8)]
    ordered = journeys.journey_order(tracks, 'time_machine', 4, lambda row: row)
    assert [row['decade'] for row in ordered] == [2000] * 4 + [2020] * 4
    assert not artist_variety.members(ordered[3]) & artist_variety.members(ordered[4])


@pytest.mark.parametrize('scarce', [False, True])
def test_album_companions_search_past_a_page_of_collaborations_and_fill_scarce_pools(monkeypatch, scarce):
    from yt_playlist.rec import album_choices
    # Every high-ranked track has a different primary artist, but shares its
    # second performer. Alternatives only appear after the first read page.
    tracks = [{**song(i, [str(i), 'shared'] if i < 130 or scarce else [str(i)]),
               'recording_id': i + 1, 'video_id': f'v{i}'} for i in range(150)]
    pages = []

    def page(_surface, *, after, limit, **_kwargs):
        pages.append(after)
        rows = tracks[after:after + limit]
        return {'tracks': rows, 'after': rows[-1]['recording_id'] if rows else after,
                'exhausted': after + limit >= len(tracks)}

    store = SimpleNamespace(_read_only=True, recommendations=SimpleNamespace(
        head=lambda _surface: {'id': 1}, recording_metadata_page=page))
    monkeypatch.setattr(album_choices.rec_params, 'get_param', lambda *_args: 2)
    keys = {track['key']: index for index, track in enumerate(tracks)}
    # Scores decrease with the key and preserve the first-page priority.
    matrix = np.asarray([[1., i / 150] for i in range(150)])
    selected, = album_choices._stored_companions(store, [{'keys': ['0']}], np.array([[1., 0.]]), matrix, keys)
    assert len(selected) == len({row['key'] for row in selected}) == 12
    assert '0' not in {row['key'] for row in selected}
    assert pages == [0, 128]
    assert sum('shared' in row['artist_ids'] for row in selected) == (12 if scarce else 2)
