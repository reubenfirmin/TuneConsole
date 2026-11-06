"""Real alternate search distinguishes failed reads, empty results and validation."""
import json
from functools import partial

import pytest

from tests.test_song_actions import songs
from yt_playlist.library import playlist_catalog
from yt_playlist.util.retry import with_retry


@pytest.mark.parametrize('kind', ['json', 'value'])
def test_failed_provider_search_is_not_reported_as_successful_empty(songs, monkeypatch, kind, caplog):
    calls = []
    diagnostic = 'internal-diagnostic-879: alternate response decoding failed'
    def fail(query, filter=None):
        calls.append(filter)
        if kind == 'json':
            raise json.JSONDecodeError(diagnostic, 'invalid response', 0)
        raise ValueError(diagnostic)
    monkeypatch.setattr(playlist_catalog, 'with_retry', partial(with_retry, sleep=lambda delay: None))
    url = f"/playlist/{songs['source']}/alternates?video_id=owned-song"
    rows = songs['store'].charts.playlist_tracks_detail(songs['source'])
    with monkeypatch.context() as failed:
        failed.setattr(songs['main'], 'search', fail)
        response = songs['http'].get(url)
    assert response.status_code == 503 and response.content == b''
    assert 'x-alternate-results' not in response.headers
    assert diagnostic in caplog.text and calls == [None] * 3 + ['songs'] * 3 + ['videos'] * 3
    assert songs['store'].charts.playlist_tracks_detail(songs['source']) == rows
    assert not songs['main'].created and not songs['main'].added and not songs['main'].deleted
    recovered = songs['http'].get(url)
    assert recovered.status_code == 200 and recovered.headers['x-alternate-results'] == 'true'
    assert 'No other versions found.' in recovered.text


@pytest.mark.parametrize('fault,code', [('membership', 'playlist_song_missing'),
                                       ('account', 'playlist_account_disconnected')])
def test_actual_alternate_validation_returns_only_fixed_codes(songs, fault, code):
    video = 'owned-song' if fault == 'account' else 'not-a-playlist-song'
    if fault == 'account':
        songs['clients'].pop(songs['main_id'])
    response = songs['http'].get(f"/playlist/{songs['source']}/alternates", params={'video_id': video})
    assert response.status_code == 422 and response.json() == {'validation': code}
    assert not songs['main'].created and not songs['main'].added and not songs['main'].deleted


def test_partial_provider_failure_keeps_real_versions_instead_of_claiming_empty(songs, monkeypatch):
    def search(query, filter=None):
        if filter == 'videos':
            raise ValueError('internal-diagnostic-879: video lookup failed')
        return [{'videoId': 'live-version', 'title': 'Owned Song (Live)',
                 'artists': [{'name': 'Artist'}], 'duration_seconds': 240}]
    monkeypatch.setattr(playlist_catalog, 'with_retry', partial(with_retry, sleep=lambda delay: None))
    monkeypatch.setattr(songs['main'], 'search', search)
    response = songs['http'].get(f"/playlist/{songs['source']}/alternates?video_id=owned-song")
    assert response.status_code == 200 and response.headers['x-alternate-results'] == 'true'
    assert 'Owned Song (Live)' in response.text and response.text.count('name="track"') == 1
    assert 'No other versions found.' not in response.text and 'internal-diagnostic' not in response.text
    assert not songs['main'].created and not songs['main'].added and not songs['main'].deleted
