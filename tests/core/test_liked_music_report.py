"""Original Music item identities may differ from their substituted playback IDs."""
import json
from itertools import chain, repeat

import pytest

from tests.core.test_playlist_page_accounting import first, item
from tests.test_saved_album_snapshot import _production_client
from yt_playlist.core import playlist_snapshot
from yt_playlist.core import youtube_playlist_report as youtube


def test_report_compares_explicit_original_ids_without_exposing_private_values(monkeypatch):
    raw = item('PRIVATE_PLAYBACK')
    data = raw['musicResponsiveListItemRenderer']
    data['overlay'] = {'musicItemThumbnailOverlayRenderer': {'content': {
        'musicPlayButtonRenderer': {'playNavigationEndpoint': {
            'watchEndpoint': {'videoId': 'PRIVATE_PLAYBACK', 'playlistId': 'LM'}}}}}}
    data['playlistItemData'] = {'videoId': 'PRIVATE_ORIGINAL'}
    data['menu'] = {'menuRenderer': {'items': [], 'topLevelButtons': [{'likeButtonRenderer': {
        'target': {'videoId': 'PRIVATE_ORIGINAL'}, 'likeStatus': 'LIKE'}}]}}
    music_page = first([raw], count=1)
    player = {'playabilityStatus': {'status': 'UNPLAYABLE', 'reason': 'PRIVATE_REASON'}}
    client, bridge = _production_client(chain(repeat(music_page, 3), repeat(player)))
    monkeypatch.setattr(youtube, 'youtube_playlist_snapshot', lambda *_args, **_kwargs: {
        'tracks': [{'videoId': value} for value in ('PRIVATE_ORIGINAL', 'PRIVATE_HIDDEN')],
        'pages': 1, 'playlist_id_verified': True})
    report = client.playlist_read_report('LM')
    assert report['ordinary']['audit']['unidentified'] == 0
    music_alias = report['youtube_music_alias']
    assert music_alias['additional_video_ids'] == 2
    assert music_alias['missing_music_video_ids'] == 1
    identities = music_alias['item_identities']
    for source in ('playlist_item', 'like_target'):
        assert identities['sources'][source] == {
            'present_rows': 1, 'distinct_video_ids': 1, 'different_playback_rows': 1,
            'additional_video_ids': 1, 'missing_video_ids': 0}
    assert identities['unmatched_source'] == 'playlist_item'
    assert identities['unmatched_music_metadata']['playability'] == {'UNPLAYABLE': 1}
    assert 'PRIVATE_' not in json.dumps(report)
    assert [body['browseId'] for _, body in bridge.calls[:3]] == ['VLLM'] * 3


def test_normal_playlist_read_does_not_collect_diagnostic_identity_rows(monkeypatch):
    def collect(*args):
        pytest.fail('Ordinary playlist reads must skip diagnostic identity collection')

    monkeypatch.setattr(playlist_snapshot, '_item_identities', collect)
    client, _ = _production_client([first([item('song')], count=1)])
    detail = client.get_playlist('PL', limit=None)
    assert [track['playlistVideoId'] for track in detail['tracks']] == ['song']


def test_incomplete_original_identity_coverage_does_not_inspect_unmatched_items():
    report = youtube._identity_report(None, [
        {'playback': 'PRIVATE_PLAYBACK', 'playlist_item': 'PRIVATE_ORIGINAL'},
        {'playback': 'PRIVATE_MISSING'},
    ], [{}, {}], {'PRIVATE_ORIGINAL', 'PRIVATE_HIDDEN'})
    assert 'unmatched_source' not in report
    assert report['sources']['playlist_item']['present_rows'] == 1
    assert 'PRIVATE_' not in json.dumps(report)
