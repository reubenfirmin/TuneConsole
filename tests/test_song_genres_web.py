"""Per-song genre provenance and lookup through the metadata editor's services."""
import time
import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.web.app import create_app


def _client(store):
    iid = store.accounts.upsert_account("main", None, True)
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                      base_url="http://127.0.0.1"), iid


def _seed(store, iid):
    pid = store.playlists.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    tid = store.tracks.upsert_track("v1", "Hyperballad", "Bjork", "Post", 200)
    store.playlists.set_playlist_tracks(pid, [tid])
    store.enrichment.apply_provider_fields(tid, 'musicbrainz', {'genre': 'Electronic', 'year': '1995'})
    store.enrichment_state.log_enrichment(tid, "run", "musicbrainz", "genre", "Electronic", now=1.0)
    store.enrichment_state.log_enrichment(tid, "run", "discogs", "genre", "Art Pop", now=1.0)
    return pid, tid


def test_metadata_exposes_genre_sources_and_current_value(store):
    client, iid = _client(store)
    _pid, tid = _seed(store, iid)
    data = client.get('/songs/metadata', params={'video_id': 'v1'}).json()
    assert data['metadata']['title'] == 'Hyperballad'
    assert data['metadata']['genre'] == 'Electronic'
    assert data['genre_options'] == [
        {'value': 'Electronic', 'providers': ['musicbrainz']},
        {'value': 'Art Pop', 'providers': ['discogs']},
    ]


def test_provider_agreement_is_one_genre_choice(store):
    client, iid = _client(store)
    _pid, tid = _seed(store, iid)
    store.enrichment_state.log_enrichment(tid, "later", "lastfm", "genre", "Electronic", now=2.0)
    provenance = store.enrichment_reports.genre_provenance(tid)
    electronic = next(o for o in provenance["options"] if o["value"] == "Electronic")
    assert electronic["providers"] == ["musicbrainz", "lastfm"]
    data = client.get('/songs/metadata', params={'video_id': 'v1'}).json()
    assert [option for option in data['genre_options'] if option['value'] == 'Electronic'] == [
        {'value': 'Electronic', 'providers': ['musicbrainz', 'lastfm']}]


def test_single_track_lookup_publishes_metadata_suggestions(store, monkeypatch):
    client, iid = _client(store)
    pid = store.playlists.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    tid = store.tracks.upsert_track("v1", "Unknown", "Artist", None, 200)
    store.playlists.set_playlist_tracks(pid, [tid])
    assert client.get('/songs/metadata', params={'video_id': 'v1'}).json()['genre_options'] == []

    def lookup(_store, tracks, _config, _progress):
        _store.enrichment_state.log_enrichment(tracks[0]["id"], "lookup", "discogs", "genre", "Ambient", now=2.0)

    monkeypatch.setattr("yt_playlist.providers.waterfall.run_waterfall", lookup)
    response = client.post(f"/track/{tid}/genre-lookup")
    assert response.status_code == 200 and response.json()["job_id"]
    job = client.app.state.ctx.jobs.get(response.json()["job_id"])
    deadline = time.time() + 1
    while not job.done and time.time() < deadline:
        time.sleep(.01)
    assert job.done
    data = client.get('/songs/metadata', params={'video_id': 'v1'}).json()
    assert data['genre_options'] == [{'value': 'Ambient', 'providers': ['discogs']}]


@pytest.mark.parametrize('failure', ['exception', 'provider-warning', 'artwork-warning'])
def test_lookup_stream_keeps_diagnostics_in_logs_and_internal_state(store, monkeypatch, caplog, failure):
    diagnostic = 'sqlite3.OperationalError: genre lease corrupt [review-879-genre-stream]'
    client, iid = _client(store)
    _pid, tid = _seed(store, iid)

    def lookup(_store, _tracks, _config, progress):
        if failure == 'exception':
            raise RuntimeError(diagnostic)
        progress({'type': 'info', 'warning': True, 'text': diagnostic,
                  'provider': diagnostic, 'error': diagnostic})
        if failure == 'artwork-warning':
            progress({'type': 'info', 'warning': True, 'text': 'Cover art lookup failed: ' + diagnostic})
        progress({'type': 'track', 'i': 1, 'n': 1, 'text': diagnostic})
        progress({'type': 'done', 'text': diagnostic})

    monkeypatch.setattr('yt_playlist.providers.waterfall.run_waterfall', lookup)
    with client:
        started = client.post(f'/track/{tid}/genre-lookup')
        assert started.status_code == 200
        job_id = started.json()['job_id']
        response = client.get(f'/track/genre-lookup/events/{job_id}')
        assert response.status_code == 200
        assert diagnostic not in response.text
        events = [json.loads(line.removeprefix('data: ')) for line in response.text.splitlines()
                  if line.startswith('data: ')]
        assert events[-1] == {'type': 'end', 'error': failure == 'exception'}
        assert diagnostic in caplog.text
        job = client.app.state.ctx.jobs.get(job_id)
        assert job.done
        assert job.error == (diagnostic if failure == 'exception' else None)
        if failure != 'exception':
            assert any(event['warning'] for event in events[:-1])
            assert next(event for event in events if event['type'] == 'track')['i'] == 1
        assert store.enrichment_reports.genre_provenance(tid)['current'] == 'Electronic'
