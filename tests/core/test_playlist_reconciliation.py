"""Filtered Music rows need independent, stable occurrence proof before replacement."""
from copy import deepcopy

import pytest

from yt_playlist.core import playlist_reconciliation as reconciliation
from yt_playlist.core.album_snapshot import LibrarySnapshotError
from yt_playlist.library.playlist_snapshot import is_complete_playlist
from yt_playlist.library.sync import _complete_rating_context


def music():
    return {'id': 'PLfixture', 'trackCount': 3, 'tracks': [
        {'setVideoId': 'first', 'videoId': 'music-alternate', 'likeStatus': 'LIKE'},
        {'setVideoId': 'last', 'videoId': 'repeated', 'likeStatus': 'INDIFFERENT'}],
        '_readAudit': {'pages': 1, 'raw_items': 2, 'omitted': 0,
                       'unidentified': 0, 'exhausted': True}}


def youtube():
    return {'pages': 2, 'playlist_id_verified': True, 'tracks': [
        {'setVideoId': 'first', 'videoId': 'youtube-original'},
        {'setVideoId': 'hidden', 'videoId': 'repeated',
         'title': {'simpleText': 'Hidden track'},
         'shortBylineText': {'runs': [{'text': 'Uploader'}]}, 'lengthSeconds': '123'},
        {'setVideoId': 'last', 'videoId': 'repeated'}]}


def reconcile(monkeypatch, detail=None, remote=None, latest=None):
    detail = music() if detail is None else detail
    remote = youtube() if remote is None else remote
    latest = deepcopy(detail) if latest is None else latest
    monkeypatch.setattr(reconciliation, 'youtube_playlist_snapshot', lambda *_: remote)
    return reconciliation.reconcile_playlist(None, 'PLfixture', detail, lambda: latest)


def test_verified_occurrences_preserve_music_substitution_duplicates_and_unknown_rating(monkeypatch):
    result = reconcile(monkeypatch)
    assert is_complete_playlist(result)
    assert [row['videoId'] for row in result['tracks']] == [
        'music-alternate', 'repeated', 'repeated']
    assert [row['setVideoId'] for row in result['tracks']] == ['first', 'hidden', 'last']
    assert result['tracks'][0]['likeStatus'] == 'LIKE'
    hidden = result['tracks'][1]
    assert hidden['title'] == 'Hidden track' and hidden['duration_seconds'] == 123
    assert 'likeStatus' not in hidden and 'isAvailable' not in hidden
    assert not _complete_rating_context(result)
    assert result['_readAudit']['hidden_rows'] == 1


@pytest.mark.parametrize('problem', ['wrong-playlist', 'count', 'missing-handle',
                                    'repeated-handle', 'missing-music-position', 'order'])
def test_unverified_youtube_membership_keeps_music_partial(monkeypatch, problem):
    remote = youtube()
    if problem == 'wrong-playlist':
        remote['playlist_id_verified'] = False
    elif problem == 'count':
        remote['tracks'].pop()
    elif problem == 'missing-handle':
        del remote['tracks'][1]['setVideoId']
    elif problem == 'repeated-handle':
        remote['tracks'][1]['setVideoId'] = 'first'
    elif problem == 'missing-music-position':
        remote['tracks'][2]['setVideoId'] = 'other'
    else:
        remote['tracks'].reverse()
    result = reconcile(monkeypatch, remote=remote)
    assert result['tracks'] == music()['tracks']
    assert not is_complete_playlist(result)


@pytest.mark.parametrize('problem', ['count', 'video', 'order', 'parser-omission'])
def test_music_change_during_verification_keeps_new_read_partial(monkeypatch, problem):
    latest = music()
    if problem == 'count':
        latest['trackCount'] = 4
    elif problem == 'video':
        latest['tracks'][0]['videoId'] = 'changed'
    elif problem == 'order':
        latest['tracks'].reverse()
    else:
        latest['_readAudit']['omitted'] = 1
    result = reconcile(monkeypatch, latest=latest)
    assert result['tracks'] == latest['tracks']
    assert not is_complete_playlist(result)


@pytest.mark.parametrize('problem', ['unidentified', 'omitted', 'not-exhausted',
                                    'missing-handle', 'repeated-handle', 'wrong-playlist'])
def test_ambiguous_music_read_never_requests_youtube(monkeypatch, problem):
    detail = music()
    if problem in {'unidentified', 'omitted'}:
        detail['_readAudit'][problem] = 1
    elif problem == 'not-exhausted':
        detail['_readAudit']['exhausted'] = False
    elif problem == 'missing-handle':
        del detail['tracks'][0]['setVideoId']
    elif problem == 'repeated-handle':
        detail['tracks'][1]['setVideoId'] = 'first'
    else:
        detail['id'] = 'PLother'
    def forbidden(*_):
        pytest.fail('Ambiguous Music membership must not attempt reconciliation')
    monkeypatch.setattr(reconciliation, 'youtube_playlist_snapshot', forbidden)
    result = reconciliation.reconcile_playlist(None, 'PLfixture', detail, forbidden)
    assert not is_complete_playlist(result)


def test_verification_failure_retains_music_without_private_error_text(monkeypatch):
    def unavailable(*_):
        raise LibrarySnapshotError('youtube-playlist', 'unavailable', {}, 1)
    monkeypatch.setattr(reconciliation, 'youtube_playlist_snapshot', unavailable)
    detail = music()
    result = reconciliation.reconcile_playlist(None, 'PLfixture', detail, lambda: music())
    assert result is detail and not is_complete_playlist(result)
    assert result['_readAudit']['reconciliation'] == 'YouTube membership verification unavailable'
