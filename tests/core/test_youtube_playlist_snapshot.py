"""Only a scoped, exhausted YouTube occurrence list can fill filtered Music rows."""
from copy import deepcopy

import pytest

from tests.core.test_playlist_page_accounting import first as music_page, item, page, token
from tests.test_saved_album_snapshot import _production_client
from yt_playlist.core.album_snapshot import LibrarySnapshotError
from yt_playlist.core.youtube_playlist_report import youtube_playlist_snapshot, _layout_probe
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


def lockup(video_id, playlist_id='LM'):
    return {'lockupViewModel': {
        'contentId': video_id, 'contentType': 'LOCKUP_CONTENT_TYPE_VIDEO',
        'rendererContext': {'commandContext': {'onTap': {'innertubeCommand': {
            'watchEndpoint': {'videoId': video_id, 'playlistId': playlist_id}}}}}}}


def modern(contents, playlist_id='LM'):
    return {
        'header': {'playlistHeaderRenderer': {'playlistId': playlist_id}},
        'contents': {'twoColumnBrowseResultsRenderer': {'tabs': [{'tabRenderer': {
            'selected': True, 'content': {'sectionListRenderer': {'contents': [
                {'itemSectionRenderer': {'contents': contents}}]}}}}]}}}


def view_token(value):
    return {'continuationItemViewModel': {'continuationCommand': {'innertubeCommand': {
        'continuationCommand': {'token': value}}}}}


@pytest.mark.parametrize('envelope', ['onResponseReceivedActions', 'onResponseReceivedEndpoints'])
def test_modern_continuation_controls_fetch_every_playlist_page(envelope):
    continuation = page([lockup('second'), view_token('PRIVATE_NEXT')])
    continuation[envelope] = continuation.pop('onResponseReceivedActions')
    client, bridge = _production_client([
        modern([lockup('first'), view_token('PRIVATE_TOKEN')]),
        continuation, page([lockup('last')]),
    ])
    result = youtube_playlist_snapshot(client, 'LM')
    assert result['tracks'] == [{'videoId': value} for value in ('first', 'second', 'last')]
    assert result['pages'] == 3 and result['playlist_id_verified'] is True
    assert bridge.calls[1][1]['continuation'] == 'PRIVATE_TOKEN'
    assert bridge.calls[2][1]['continuation'] == 'PRIVATE_NEXT'


@pytest.mark.parametrize('problem', ['missing', 'invalid', 'middle', 'repeated', 'ambiguous', 'mixed-row'])
def test_modern_continuations_never_certify_a_truncated_snapshot(problem):
    last = page([lockup('last')])
    continuation = view_token('PRIVATE_TOKEN')
    if problem == 'missing':
        continuation = {'continuationItemViewModel': {}}
    elif problem == 'invalid':
        continuation = view_token(1)
    elif problem == 'repeated':
        last = page([view_token('PRIVATE_TOKEN')])
    elif problem == 'ambiguous':
        last['onResponseReceivedEndpoints'] = deepcopy(last['onResponseReceivedActions'])
    elif problem == 'mixed-row':
        continuation.update(lockup('PRIVATE_VIDEO'))
    rows = [lockup('first'), continuation]
    if problem == 'middle':
        rows.reverse()
    client, _ = _production_client([modern(rows), last])
    with pytest.raises(LibrarySnapshotError) as error:
        youtube_playlist_snapshot(client, 'LM')
    assert 'PRIVATE_' not in str(error.value)


def test_modern_liked_music_reads_only_selected_playlist_rows_and_all_continuations():
    response = modern([lockup('visible'), token('PRIVATE_TOKEN')])
    response['sidebar'] = modern([lockup('not-a-member')])
    client, _ = _production_client([response, page([lockup('hidden')])])
    result = youtube_playlist_snapshot(client, 'LM')
    assert result['tracks'] == [{'videoId': 'visible'}, {'videoId': 'hidden'}]
    assert result['playlist_id_verified'] is True
    assert result['pages'] == 2


def test_modern_playlist_metadata_does_not_exhaust_the_legacy_search_budget():
    rows = [lockup(f'song-{i}') for i in range(100)]
    for row in rows:
        row['lockupViewModel']['metadata'] = [{'title': 'PRIVATE_TITLE'}] * 150
    response = modern(rows)
    client, _ = _production_client([response])
    result = youtube_playlist_snapshot(client, 'LM')
    assert len(result['tracks']) == 100
    assert result['playlist_id_verified'] is True


def test_modern_invalid_row_reports_its_stage_without_private_values():
    response = modern([lockup('visible'), lockup('PRIVATE_VIDEO')])
    rows = response['contents']['twoColumnBrowseResultsRenderer']['tabs'][0]['tabRenderer'][
        'content']['sectionListRenderer']['contents'][0]['itemSectionRenderer']['contents']
    del rows[1]['lockupViewModel']['rendererContext']
    client, _ = _production_client([response])
    with pytest.raises(LibrarySnapshotError) as error:
        youtube_playlist_snapshot(client, 'LM')
    assert error.value.layout_probe['validation_stage'] == 'contents.video'
    assert error.value.layout_probe['item_index'] == 1
    assert error.value.layout_probe['failed_row']['.contentType.is_video'] is True
    assert '.rendererContext' not in error.value.layout_probe['failed_row']
    assert 'PRIVATE_' not in str(error.value.layout_probe)


@pytest.mark.parametrize('mutation', ['header', 'playlist', 'video', 'type', 'selected', 'section'])
def test_modern_liked_music_rejects_ambiguous_membership(mutation):
    response = modern([lockup('PRIVATE_VIDEO')])
    tab = response['contents']['twoColumnBrowseResultsRenderer']['tabs'][0]['tabRenderer']
    view = tab['content']['sectionListRenderer']['contents'][0]['itemSectionRenderer']['contents'][0]['lockupViewModel']
    if mutation == 'header':
        response['header']['playlistHeaderRenderer']['playlistId'] = 'LL'
    elif mutation == 'type':
        view['contentType'] = 'LOCKUP_CONTENT_TYPE_PLAYLIST'
    elif mutation in {'playlist', 'video'}:
        view['rendererContext']['commandContext']['onTap']['innertubeCommand']['watchEndpoint'][
            'playlistId' if mutation == 'playlist' else 'videoId'] = 'PRIVATE_OTHER'
    elif mutation == 'selected':
        response['contents']['twoColumnBrowseResultsRenderer']['tabs'].append(deepcopy(
            response['contents']['twoColumnBrowseResultsRenderer']['tabs'][0]))
    else:
        tab['content']['sectionListRenderer']['contents'].append({'itemSectionRenderer': {'contents': []}})
    client, _ = _production_client([response])
    with pytest.raises(LibrarySnapshotError) as error:
        youtube_playlist_snapshot(client, 'LM')
    assert 'PRIVATE_' not in str(error.value)


def test_modern_layout_probe_exposes_only_bounded_structure_and_identity_comparisons():
    response = modern([lockup('PRIVATE_VIDEO')])
    response['header']['playlistHeaderRenderer']['title'] = 'PRIVATE_TITLE'
    view = response['contents']['twoColumnBrowseResultsRenderer']['tabs'][0]['tabRenderer'][
        'content']['sectionListRenderer']['contents'][0]['itemSectionRenderer']['contents'][0]['lockupViewModel']
    view['PRIVATE_KEY'] = {'cookie': 'PRIVATE_TOKEN'}
    result = _layout_probe(response, 'LM')
    import json
    encoded = json.dumps(result)
    assert 'PRIVATE_' not in encoded and len(encoded) < 4096
    assert result['header_id_matches'] is True
    assert result['first_row']['.rendererContext.commandContext.onTap.innertubeCommand.watchEndpoint.playlistId.matches_request'] is True
