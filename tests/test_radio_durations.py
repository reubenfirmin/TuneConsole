"""Confirmed Radio uploads retain their exact duration in the session and catalog."""
import pytest
from fastapi import FastAPI
from tests.template_support import web_templates
from fastapi.testclient import TestClient

from yt_playlist.web.radio_playlist_io import readback_durations
from yt_playlist.web.routes import radio_session as radio_routes
from tests.radio_session_support import poc, play


def with_remote_durations(remote, **durations):
    read = remote.get_playlist

    def detail(*args, **kwargs):
        result = read(*args, **kwargs)
        for row in result['tracks']:
            if row['videoId'] in durations:
                row.update(durations[row['videoId']])
        return result

    remote.get_playlist = detail


def test_initial_and_appended_radio_times_survive_readback_and_render(poc):
    c, remote, *_ = poc
    with_remote_durations(remote, v1={'duration_seconds': 363}, v4={'duration': '6:42'})
    c.start()
    assert c.status()['tracks'][0]['duration'] == 363
    play(c, 'v3')
    assert c.status()['tracks'][-1]['duration'] == 402
    # GET /radio/playlist reads the catalog, not the in-memory candidate dict.
    c.ctx.templates = web_templates()
    app = FastAPI()
    app.include_router(radio_routes.build(c.ctx))
    play(c, 'v4')
    with TestClient(app) as client:
        response = client.get('/radio/playlist')
    assert response.status_code == 200
    assert '6:03' in response.text and '6:42' in response.text
    assert 'Duration unavailable' in response.text


def test_append_looks_up_missing_duration_once_and_persists_it(poc):
    c, remote, *_ = poc
    calls = []
    remote.get_song = lambda video: calls.append(video) or {'videoDetails': {'lengthSeconds': '214'}}
    c.start()
    play(c, 'v3')
    assert calls == ['v4']
    assert c.status()['tracks'][-1]['duration'] == 214
    detail = c.ctx.store.charts.video_tracks_detail(['v4'])
    assert detail[0]['duration'] == 214
    play(c, 'v3')
    assert calls == ['v4']


def test_unavailable_lookup_does_not_block_or_repeat_on_confirmed_songs(poc):
    c, remote, *_ = poc
    calls = []
    remote.get_song = lambda video: calls.append(video) or {}
    c.start()
    play(c, 'v3')
    play(c, 'v3')
    assert calls == ['v4']
    assert c.status()['tracks'][-1].get('duration') is None
    assert remote.ids == ['v1', 'v2', 'v3', 'v4']


def test_same_queue_accepts_later_duration_without_resetting_playback(poc):
    c, remote, *_ = poc
    c.start()
    play(c, 'v1')
    session = c.session
    before = session.metadata_revision
    updated = readback_durations(session.tracks, [{'videoId': 'v1', 'duration': '4:02'}])
    session.accept_tracks(updated, c.ctx.now_fn())
    assert session.tracks[0]['duration'] == 242
    assert session.position == 0 and session.furthest == 0
    assert session.metadata_revision == before + 1


@pytest.mark.parametrize('invalid', [None, '', 0, -1, True, 'nan', float('inf'), {}, 'bad'])
def test_bad_durations_are_unknown_and_never_borrowed_from_another_upload(invalid):
    tracks = [{'video_id': 'exact', 'duration': invalid}, {'video_id': 'unknown'}]
    result = readback_durations(tracks, [
        {'videoId': 'exact', 'duration_seconds': invalid},
        {'videoId': 'different', 'duration_seconds': 400},
    ])
    assert result[0]['duration'] is None
    assert result[1].get('duration') is None


def test_readback_retains_known_duration_and_accepts_numeric_strings():
    tracks = [{'video_id': 'known', 'duration': 203}, {'video_id': 'missing'}]
    result = readback_durations(tracks, [
        {'videoId': 'known', 'duration_seconds': 999},
        {'videoId': 'missing', 'duration_seconds': '245'},
    ])
    assert [track['duration'] for track in result] == [203, 245]
    assert 'duration' not in tracks[1], 'readback must not mutate a pending plan'


def test_replacement_from_an_older_candidate_cannot_erase_a_confirmed_duration():
    tracks = [{'video_id': 'known'}]
    result = readback_durations(tracks, [{'videoId': 'known'}],
                               previous=[{'video_id': 'known', 'duration': 245}])
    assert result[0]['duration'] == 245
