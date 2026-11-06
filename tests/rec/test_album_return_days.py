"""Date unions consume immutable choice DTOs; alias reconciliation stays integrated."""
from copy import deepcopy
import sqlite3

import pytest

from yt_playlist.rec import choice_summary


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Return-day arithmetic must not open SQLite')
    monkeypatch.setattr(sqlite3, 'connect', forbidden)


@pytest.mark.parametrize('requests,unverified', [
    pytest.param([['preferred'], ['preferred']], {'preferred': 2}, id='preferred'),
    pytest.param([['alias'], ['alias']], {'alias': 2}, id='alias'),
    pytest.param([['preferred'], ['alias']], {'preferred': 1, 'alias': 1}, id='split-days'),
    pytest.param([['preferred', 'alias'], ['preferred', 'alias']],
                 {'preferred': 2, 'alias': 2}, id='overlapping-days'),
])
def test_confirmed_return_dates_union_only_after_verified_alias_mapping(requests, unverified):
    tracks = [{'key': f'track {index}|artist', 'video_id': f'video-{index}'} for index in range(9)]
    offers, responses = {}, []
    for day, aliases in enumerate(requests):
        at = 1_700_000_000 + day * 86400
        for index, alias in enumerate(aliases):
            identity = f'return-{day}-{index}'
            offers[identity] = {'id': identity, 'format': 'albums',
                                'options': [{'id': alias, 'tracks': deepcopy(tracks)}]}
            responses.extend([
                {'id': identity + ':choice', 'opportunity_id': identity, 'option_id': alias,
                 'kind': 'choice', 'at': at + 1},
                {'id': identity + ':start', 'opportunity_id': identity, 'option_id': alias,
                 'kind': 'start', 'at': at + 2, 'details': {'video_id': 'video-0'}},
            ])
    original = deepcopy((offers, responses))
    assert choice_summary.album_track_days(offers, responses, 3600) == {
        alias: {'track 0|artist': days} for alias, days in unverified.items()}
    assert choice_summary.album_track_days(offers, responses, 3600,
        album_ids={'preferred': 'preferred', 'alias': 'preferred'}) == {
            'preferred': {'track 0|artist': 2}}
    assert (offers, responses) == original
