"""Original Music item identities may differ from their substituted playback IDs."""
from copy import deepcopy
import json

from tests.core.test_playlist_page_accounting import first, item
from tests.test_saved_album_snapshot import _production_client
from yt_playlist.core import youtube_playlist_report as youtube
from yt_playlist.core.playlist_snapshot import _PlaylistRead, _complete_playlist


def test_report_compares_explicit_original_ids_without_exposing_private_values(monkeypatch):
    raw = item('PRIVATE_PLAYBACK')
    data = raw['musicResponsiveListItemRenderer']
    data['overlay'] = {'musicItemThumbnailOverlayRenderer': {'content': {
        'musicPlayButtonRenderer': {'playNavigationEndpoint': {
            'watchEndpoint': {'videoId': 'PRIVATE_PLAYBACK', 'playlistId': 'LM'}}}}}}
    data['playlistItemData'] = {'videoId': 'PRIVATE_ORIGINAL'}
    data['menu'] = {'menuRenderer': {'items': [], 'topLevelButtons': [{'likeButtonRenderer': {
        'target': {'videoId': 'PRIVATE_ORIGINAL'}, 'likeStatus': 'LIKE'}}]}}
    client, _ = _production_client([first([raw], count=2)])
    reader = _PlaylistRead(client, collect_identities=True)
    detail = _complete_playlist(reader, 'LM')
    assert detail['tracks'][0]['videoId'] == 'PRIVATE_PLAYBACK'
    assert detail['_readAudit']['unidentified'] == 0
    before = deepcopy(detail)
    monkeypatch.setattr(youtube, 'youtube_playlist_snapshot', lambda *_args, **_kwargs: {
        'tracks': [{'videoId': value} for value in ('PRIVATE_ORIGINAL', 'PRIVATE_HIDDEN')],
        'pages': 1, 'playlist_id_verified': True})
    inspected = []

    def song(video_id):
        inspected.append(video_id)
        return {'playabilityStatus': {'status': 'UNPLAYABLE', 'reason': 'PRIVATE_REASON'}}

    monkeypatch.setattr(client, 'get_song', song)
    report = youtube.youtube_playlist_report(client, 'LM', detail['tracks'],
                                               music_identity_rows=reader.identity_rows)
    identities = report['item_identities']
    for source in ('playlist_item', 'like_target'):
        assert identities['sources'][source] == {
            'present_rows': 1, 'distinct_video_ids': 1, 'different_playback_rows': 1,
            'additional_video_ids': 1, 'missing_video_ids': 0}
    assert identities['unmatched_source'] == 'playlist_item'
    assert identities['unmatched_music_metadata']['playability'] == {'UNPLAYABLE': 1}
    assert 'PRIVATE_' not in json.dumps(report)
    assert detail == before
    assert reader.identity_rows[0]['playlist_item'] == 'PRIVATE_ORIGINAL'


def test_normal_playlist_read_does_not_collect_diagnostic_identity_rows():
    client, _ = _production_client([first([item('song')], count=1)])
    reader = _PlaylistRead(client)
    assert len(_complete_playlist(reader, 'LM')['tracks']) == 1
    assert reader.identity_rows is None


def test_incomplete_original_identity_coverage_does_not_inspect_unmatched_items():
    report = youtube._identity_report(None, [
        {'playback': 'PRIVATE_PLAYBACK', 'playlist_item': 'PRIVATE_ORIGINAL'},
        {'playback': 'PRIVATE_MISSING'},
    ], [{}, {}], {'PRIVATE_ORIGINAL', 'PRIVATE_HIDDEN'})
    assert 'unmatched_source' not in report
    assert report['sources']['playlist_item']['present_rows'] == 1
    assert 'PRIVATE_' not in json.dumps(report)
