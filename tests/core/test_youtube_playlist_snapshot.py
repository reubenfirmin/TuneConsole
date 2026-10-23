"""Only a scoped, exhausted YouTube occurrence list can fill filtered Music rows."""
from copy import deepcopy

import pytest

from tests.core.test_playlist_page_accounting import first as music_page, item, page, token
from tests.test_saved_album_snapshot import _production_client
from yt_playlist.core.album_snapshot import LibrarySnapshotError
from yt_playlist.core.youtube_playlist_report import youtube_playlist_snapshot
from yt_playlist.library.playlist_snapshot import is_complete_playlist


def video(video_id, handle):
    return {'playlistVideoRenderer': {'videoId': video_id, 'setVideoId': handle}}


def first(contents, playlist_id='PL'):
    return {'contents': {'twoColumnBrowseResultsRenderer': {
        'tabs': [{'tabRenderer': {'content': {'sectionListRenderer': {'contents': [
            {'itemSectionRenderer': {'contents': [{'playlistVideoListRenderer': {
                'playlistId': playlist_id, 'contents': contents}}]}}]}}}}]}}}


def test_scoped_reader_preserves_duplicate_songs_and_account_context():
    response = first([video('repeated', 'one'), token('next')])
    response['recommendations'] = video('unrelated', 'not-a-member')
    client, bridge = _production_client([response, page([video('repeated', 'two')])])
    original = deepcopy(client.context)
    client.context['context']['user'] = {'onBehalfOfUser': 'fixture-account'}
    expected = deepcopy(client.context)
    result = youtube_playlist_snapshot(client, 'PL')
    assert [row['setVideoId'] for row in result['tracks']] == ['one', 'two']
    assert result['pages'] == 2 and result['playlist_id_verified'] is True
    assert client.context == expected
    for _, body in bridge.calls:
        assert body['context']['client']['clientName'] == 'WEB'
        assert body['context']['user'] == {'onBehalfOfUser': 'fixture-account'}
    assert bridge.calls[0][1]['params'] == 'wgYCCAA='
    assert original['context']['client'] == expected['context']['client']


@pytest.mark.parametrize('last', [
    {}, page(None), page([{'unknownRenderer': {}}]),
    page([video('', 'two')]), page([video('song', 'one')]),
    page([token('next')]), page([token('later'), video('song', 'two')]),
    {'responseContext': {'mainAppWebResponseContext': {'loggedOut': True}}},
])
def test_missing_ambiguous_or_repeated_page_rejects_the_entire_snapshot(last):
    client, _ = _production_client([first([video('song', 'one'), token('next')]), last])
    with pytest.raises(LibrarySnapshotError):
        youtube_playlist_snapshot(client, 'PL')


def test_explicit_wrong_playlist_is_rejected():
    client, _ = _production_client([first([video('song', 'one')], 'PLother')])
    with pytest.raises(LibrarySnapshotError):
        youtube_playlist_snapshot(client, 'PL')


def test_full_production_reader_reconciles_only_verified_missing_occurrences():
    music_item = item('music-alternate')
    music_item['musicResponsiveListItemRenderer']['menu'] = {'menuRenderer': {'items': [
        {'menuServiceItemRenderer': {'serviceEndpoint': {'playlistEditEndpoint': {'actions': [
            {'setVideoId': 'one', 'removedVideoId': 'music-alternate'}]}}}}]}}
    music = music_page([music_item], count=2)
    client, bridge = _production_client([
        music, first([video('youtube-original', 'one'), token('next')]),
        page([video('hidden', 'two')]), deepcopy(music),
    ])
    result = client.get_playlist('PL', limit=None)
    assert is_complete_playlist(result)
    assert [row['videoId'] for row in result['tracks']] == ['music-alternate', 'hidden']
    assert result['_readAudit']['hidden_rows'] == 1
    assert 'likeStatus' not in result['tracks'][1]
    assert len(bridge.calls) == 4


def test_raw_response_values_never_enter_snapshot_errors():
    client, _ = _production_client([first([{
        'unknownRenderer': {'title': 'PRIVATE_TITLE', 'token': 'PRIVATE_TOKEN'}}])])
    with pytest.raises(LibrarySnapshotError) as error:
        youtube_playlist_snapshot(client, 'PL')
    assert 'PRIVATE_' not in str(error.value)
