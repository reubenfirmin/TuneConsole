"""Strict page accounting never converts an upstream parser omission into membership proof."""
from copy import deepcopy

import pytest

from tests.test_saved_album_snapshot import _production_client
from yt_playlist.core.album_snapshot import LibrarySnapshotError
from yt_playlist.library.playlist_snapshot import is_complete_playlist


def item(video='song', *, deleted=False):
    text = 'Song deleted' if deleted else video
    return {'musicResponsiveListItemRenderer': {
        'musicItemRendererDisplayPolicy': 'MUSIC_ITEM_RENDERER_DISPLAY_POLICY_GREY_OUT',
        'flexColumns': [{'musicResponsiveListItemFlexColumnRenderer': {
            'text': {'runs': [{'text': text}]}}}],
        'playlistItemData': {'videoId': video},
    }}


def token(value):
    return {'continuationItemRenderer': {'continuationEndpoint': {
        'continuationCommand': {'token': value}}}}


def first(contents, count=0):
    header = {'title': {'runs': [{'text': 'Fixture playlist'}]},
              'thumbnail': {'musicThumbnailRenderer': {'thumbnail': {'thumbnails': []}}},
              'subtitle': {'runs': [{'text': 'Playlist'}, {'text': ' • '}, {'text': 'Artist'}]},
              'secondSubtitle': {'runs': [{'text': f'{count} songs'}]},
              'buttons': [{}, {'musicPlayButtonRenderer': {
                  'playNavigationEndpoint': {'watchEndpoint': {'playlistId': 'PL'}}}}]}
    return {'contents': {'twoColumnBrowseResultsRenderer': {
        'tabs': [{'tabRenderer': {'content': {'sectionListRenderer': {'contents': [
            {'musicResponsiveHeaderRenderer': header}]}}}}],
        'secondaryContents': {'sectionListRenderer': {'contents': [
            {'musicPlaylistShelfRenderer': {'contents': contents}}]}}}}}


def page(contents):
    return {'onResponseReceivedActions': [{'appendContinuationItemsAction': {
        'continuationItems': contents}}]}


def test_verified_empty_playlist_avoids_upstream_empty_token_index_error():
    response = first([])
    before = deepcopy(response)
    client, _ = _production_client([response])
    result = client.get_playlist('PL', limit=None)
    assert result['tracks'] == [] and is_complete_playlist(result)
    assert result['_readAudit'] == {'pages': 1, 'raw_items': 0, 'omitted': 0,
                                    'unidentified': 0, 'exhausted': True}
    assert response == before


def test_explicit_zero_count_allows_music_empty_shelf_message():
    client, _ = _production_client([first([{'messageRenderer': {'text': {'runs': [
        {'text': 'No saved episodes'}]}}}], count=0)])
    result = client.get_playlist('SE', limit=None)
    assert is_complete_playlist(result)
    assert result['_readAudit']['raw_items'] == 0


def test_notice_cannot_explain_missing_tracks_from_nonempty_header():
    client, _ = _production_client([first([{'messageRenderer': {}}], count=1)])
    with pytest.raises(LibrarySnapshotError):
        client.get_playlist('PL', limit=None)


@pytest.mark.parametrize('count', [0, 1])
def test_saved_episodes_empty_state_requires_explicit_zero_header(count):
    response = first([], count=count)
    shelf = response['contents']['twoColumnBrowseResultsRenderer']['secondaryContents'][
        'sectionListRenderer']['contents'][0]['musicPlaylistShelfRenderer']
    del shelf['contents']
    shelf['emptyState'] = {'messageRenderer': {'text': {'runs': [{'text': 'Empty episodes'}]}}}
    client, _ = _production_client([response])
    if count:
        with pytest.raises(LibrarySnapshotError):
            client.get_playlist('SE', limit=None)
    else:
        result = client.get_playlist('SE', limit=None)
        assert is_complete_playlist(result)
        assert result['_readAudit']['raw_items'] == 0


@pytest.mark.parametrize('count', [0, 1])
def test_empty_message_beside_track_shelf_requires_explicit_zero_count(count):
    response = first([], count=count)
    section = response['contents']['twoColumnBrowseResultsRenderer']['secondaryContents']['sectionListRenderer']
    del section['contents'][0]['musicPlaylistShelfRenderer']['contents']
    section['contents'].append({'messageRenderer': {'text': {'runs': [{'text': 'Empty list'}]}}})
    client, _ = _production_client([response])
    if count:
        with pytest.raises(LibrarySnapshotError):
            client.get_playlist('PL', limit=None)
    else:
        result = client.get_playlist('PL', limit=None)
        assert is_complete_playlist(result)
        assert result['_readAudit']['raw_items'] == 0


def test_deleted_only_continuation_does_not_stop_before_the_next_page():
    client, bridge = _production_client([
        first([token('one')], count=2),
        page([item('deleted', deleted=True), token('two')]),
        page([item('unidentified')]),
    ])
    result = client.get_playlist('PL', limit=None)
    assert len(bridge.calls) == 3
    assert result['_readAudit'] == {'pages': 3, 'raw_items': 2, 'omitted': 1,
                                    'unidentified': 1, 'exhausted': True}
    assert len(result['tracks']) == 1
    assert not is_complete_playlist(result)


@pytest.mark.parametrize('continuation', [
    {}, {'onResponseReceivedActions': []},
    page(None), page([{'unknownRenderer': {}}]),
    page([token('one')]),
])
def test_missing_malformed_or_repeated_pages_cannot_certify_completeness(continuation):
    client, _ = _production_client([first([token('one')], count=0), continuation])
    with pytest.raises(LibrarySnapshotError):
        client.get_playlist('PL', limit=None)


def test_unknown_header_count_does_not_hide_a_parser_omission():
    client, _ = _production_client([first([item('deleted', deleted=True)], count=1)])
    result = client.get_playlist('PL', limit=None)
    result['trackCount'] = None
    assert result['_readAudit']['omitted'] == 1
    assert not is_complete_playlist(result)


def test_diagnostics_never_contain_private_response_values():
    client, _ = _production_client([first([token('PRIVATE_TOKEN')]),
                                   page([{'unknownRenderer': {'secret': 'PRIVATE_TITLE'}}])])
    with pytest.raises(LibrarySnapshotError) as error:
        client.get_playlist('PL', limit=None)
    assert 'PRIVATE_' not in str(error.value)
