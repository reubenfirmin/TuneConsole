"""Original likes reconcile independently filtered LM views without guessing IDs."""
from copy import deepcopy
import json

import pytest

from tests.core.test_playlist_page_accounting import first, item
from tests.core.test_youtube_playlist_snapshot import lockup, modern, view_token
from tests.test_saved_album_snapshot import _production_client
from yt_playlist.core import liked_music_snapshot as liked
from yt_playlist.core.bridge import BridgeError
from yt_playlist.library.playlist_snapshot import is_complete_playlist
from yt_playlist.library.sync import _complete_rating_context


def music(originals=('first', 'music-only', 'last'), playback=None, count=4):
    playback = originals if playback is None else playback
    tracks = [{'videoId': video, 'likeStatus': 'LIKE', 'title': original}
              for original, video in zip(originals, playback)]
    identities = [{'playback': video, 'like_target': original}
                  for original, video in zip(originals, playback)]
    return {'id': None, 'trackCount': count, 'tracks': tracks,
            '_readAudit': {'pages': 1, 'raw_items': len(tracks), 'omitted': 0,
                           'unidentified': 0, 'exhausted': True}}, identities


def youtube(originals=('first', 'last', 'youtube-only')):
    return {'tracks': [{'videoId': value, 'title': {'simpleText': value}}
                       for value in originals], 'pages': 2, 'playlist_id_verified': True}


def reconcile(monkeypatch, pair=None, remote=None, latest=None):
    pair = music() if pair is None else pair
    latest = deepcopy(pair) if latest is None else latest
    calls = []

    def read(_client, playlist_id):
        calls.append(playlist_id)
        return youtube() if remote is None else remote

    monkeypatch.setattr(liked, 'youtube_playlist_snapshot', read)
    result = liked.reconcile_liked_music(None, *pair, lambda: latest)
    assert not calls or calls == ['LM']
    return result


def test_union_of_two_filtered_lm_views_preserves_exact_originals_and_playback(monkeypatch):
    pair = music(playback=('replacement', 'replacement', 'last'))
    before = deepcopy(pair)
    result = reconcile(monkeypatch, pair=pair)
    assert is_complete_playlist(result)
    assert [track['videoId'] for track in result['tracks']] == [
        'replacement', 'replacement', 'last', 'youtube-only']
    assert result['tracks'][:3] == pair[0]['tracks']
    assert result['tracks'][-1] == {'videoId': 'youtube-only', 'title': 'youtube-only'}
    assert not _complete_rating_context(result)
    assert result['_readAudit'] == {
        'pages': 1, 'raw_items': 4, 'omitted': 0, 'unidentified': 0, 'exhausted': True,
        'source': 'music-and-youtube-verified-likes', 'music_rows': 3, 'youtube_rows': 3,
        'youtube_pages': 2, 'hidden_rows': 1, 'music_only_rows': 1,
        'substituted_video_positions': 2}
    assert pair == before


def test_youtube_superset_can_fill_hidden_likes_between_music_anchors(monkeypatch):
    result = reconcile(monkeypatch, pair=music(('first', 'last'), count=4),
                       remote=youtube(('before', 'first', 'hidden', 'last')))
    assert is_complete_playlist(result)
    assert [track['videoId'] for track in result['tracks']] == ['before', 'first', 'hidden', 'last']
    assert result['_readAudit']['music_only_rows'] == 0


@pytest.mark.parametrize('problem', [
    'count', 'wrong-music-list', 'omitted', 'unidentified', 'not-exhausted',
    'raw-count', 'missing-original', 'duplicate-original', 'unknown-rating',
    'wrong-playback', 'partial-identities'])
def test_unverified_music_cannot_request_an_independent_view(monkeypatch, problem):
    pair = music()
    detail, identities = pair
    if problem == 'count':
        detail['trackCount'] = '4'
    elif problem == 'wrong-music-list':
        detail['id'] = 'LL'
    elif problem in ('omitted', 'unidentified'):
        detail['_readAudit'][problem] = 1
    elif problem == 'not-exhausted':
        detail['_readAudit']['exhausted'] = False
    elif problem == 'raw-count':
        detail['_readAudit']['raw_items'] = 4
    elif problem == 'missing-original':
        identities[0]['like_target'] = None
    elif problem == 'duplicate-original':
        identities[1]['like_target'] = identities[0]['like_target']
    elif problem == 'unknown-rating':
        detail['tracks'][0]['likeStatus'] = 'INDIFFERENT'
    elif problem == 'wrong-playback':
        identities[0]['playback'] = 'other'
    else:
        identities.pop()

    def forbidden(*_):
        pytest.fail('Ambiguous Music must not start an independent read')

    monkeypatch.setattr(liked, 'youtube_playlist_snapshot', forbidden)
    result = liked.reconcile_liked_music(None, *pair, forbidden)
    assert not is_complete_playlist(result)
    assert result['tracks'] == detail['tracks']


@pytest.mark.parametrize('problem', ['wrong-list', 'duplicate', 'empty-id',
                                    'count-short', 'count-long', 'conflicting-order',
                                    'ambiguous-order', 'no-anchor'])
def test_unverified_union_retains_music_cache_guard(monkeypatch, problem):
    remote = youtube()
    if problem == 'wrong-list':
        remote['playlist_id_verified'] = False
    elif problem == 'duplicate':
        remote['tracks'][1]['videoId'] = 'first'
    elif problem == 'empty-id':
        remote['tracks'][1]['videoId'] = ''
    elif problem == 'count-short':
        remote['tracks'].pop()
    elif problem == 'count-long':
        remote['tracks'].append({'videoId': 'extra'})
    elif problem == 'conflicting-order':
        remote['tracks'].reverse()
    elif problem == 'ambiguous-order':
        remote = youtube(('first', 'youtube-only', 'last'))
    else:
        remote = youtube(('different',))
    result = reconcile(monkeypatch, remote=remote)
    assert not is_complete_playlist(result)
    assert result['tracks'] == music()[0]['tracks']


@pytest.mark.parametrize('problem', ['count', 'original', 'playback', 'order', 'rating', 'audit'])
def test_changes_during_verification_keep_latest_read_partial(monkeypatch, problem):
    latest = music()
    if problem == 'count':
        latest[0]['trackCount'] = 5
    elif problem == 'original':
        latest[1][0]['like_target'] = 'changed'
    elif problem == 'playback':
        latest[0]['tracks'][0]['videoId'] = latest[1][0]['playback'] = 'changed'
    elif problem == 'order':
        latest[0]['tracks'].reverse()
        latest[1].reverse()
    elif problem == 'rating':
        latest[0]['tracks'][0]['likeStatus'] = 'INDIFFERENT'
    else:
        latest[0]['_readAudit']['omitted'] = 1
    result = reconcile(monkeypatch, latest=latest)
    assert result['tracks'] == latest[0]['tracks']
    assert not is_complete_playlist(result)


def test_bridge_failure_never_leaks_private_values_or_clears_membership(monkeypatch):
    def unavailable(*_):
        raise BridgeError('PRIVATE_TOKEN PRIVATE_VIDEO')

    monkeypatch.setattr(liked, 'youtube_playlist_snapshot', unavailable)
    pair = music()
    result = liked.reconcile_liked_music(None, *pair, lambda: pytest.fail('No bracket read'))
    assert result['tracks'] == pair[0]['tracks'] and not is_complete_playlist(result)
    assert 'PRIVATE_' not in json.dumps(result['_readAudit'])


def liked_row(original, playback):
    raw = item(playback)
    renderer = raw['musicResponsiveListItemRenderer']
    renderer['overlay'] = {'musicItemThumbnailOverlayRenderer': {'content': {
        'musicPlayButtonRenderer': {'playNavigationEndpoint': {
            'watchEndpoint': {'videoId': playback, 'playlistId': 'LM'}}}}}}
    renderer['menu'] = {'menuRenderer': {'items': [], 'topLevelButtons': [
        {'likeButtonRenderer': {'target': {'videoId': original}, 'likeStatus': 'LIKE'}}]}}
    return raw


def test_real_client_reconciles_original_ids_across_modern_pages_and_stable_bracket():
    initial = first([liked_row('original', 'replacement'), liked_row('last', 'last')], count=3)
    header = initial['contents']['twoColumnBrowseResultsRenderer']['tabs'][0][
        'tabRenderer']['content']['sectionListRenderer']['contents'][0][
        'musicResponsiveHeaderRenderer']
    header['buttons'][1]['musicPlayButtonRenderer']['playNavigationEndpoint'][
        'watchEndpoint']['playlistId'] = 'LM'
    hidden = lockup('hidden')
    hidden['lockupViewModel']['metadata'] = {
        'lockupMetadataViewModel': {'title': {'content': 'Unavailable upload'}}}
    client, bridge = _production_client([
        initial, modern([lockup('original'), hidden, view_token('next')]),
        {'onResponseReceivedEndpoints': [{'appendContinuationItemsAction': {
            'continuationItems': [lockup('last')]}}]}, deepcopy(initial)])
    result = client.get_playlist('LM', limit=None)
    assert is_complete_playlist(result)
    assert [track['videoId'] for track in result['tracks']] == ['replacement', 'hidden', 'last']
    assert result['tracks'][1] == {'videoId': 'hidden', 'title': 'Unavailable upload'}
    assert not _complete_rating_context(result)
    assert [body['context']['client']['clientName'] for _, body in bridge.calls] == [
        'WEB_REMIX', 'WEB', 'WEB', 'WEB_REMIX']
    assert bridge.calls[1][1]['browseId'] == 'VLLM'
    assert result['_readAudit']['hidden_rows'] == 1
