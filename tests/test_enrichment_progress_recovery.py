"""Real album/playlist jobs retain metadata and diagnostic-free replay after failures."""
import json

import pytest
from fastapi.testclient import TestClient

from tests.quiet_enrichment_support import quiet_enrichment
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('scope', ['playlist', 'album'])
@pytest.mark.parametrize('fault', ['outer', 'provider', 'artwork', 'deferred'])
def test_real_enrichment_keeps_diagnostics_internal_on_delivery_and_replay(
        store, quiet_enrichment, scope, fault, caplog):
    case = quiet_enrichment
    case.fault = fault
    app = create_app(store, lambda: case.clients, now_fn=lambda: 1000)
    start = (f'/playlist/{case.playlist}/enrich' if scope == 'playlist'
             else '/album/QUIET-ALBUM/enrich')

    def events(response):
        assert response.status_code == 200
        assert case.diagnostic not in response.text
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]

    with TestClient(app, base_url='http://127.0.0.1') as client:
        job_id = client.post(start).json()['job_id']
        delivered = events(client.get(f'/playlist/enrich/events/{job_id}'))
        job = app.state.ctx.jobs.get(job_id)
        assert job.done and delivered[-1] == {'type': 'end', 'error': fault == 'outer'}
        assert case.diagnostic in caplog.text
        assert len(case.calls) == 1
        assert job.error == (case.diagnostic if fault == 'outer' else None)
        if fault != 'outer':
            assert any(case.diagnostic in event.get('text', '')
                       for _, event in job.events.since(0)[1])
            assert any(event.get('warning') or event.get('deferred') for event in delivered)
        genres = [store.tracks.get_track_enrichment(track)[0] or '' for track in case.tracks]
        assert genres == (['Rock', ''] if fault == 'outer' else
                          ['', 'Rock'] if fault in ('provider', 'deferred') else ['Rock', 'Rock'])
        store.tracks.set_track_genre(case.tracks[0], 'Jazz')
        store.tracks.set_track_year(case.tracks[0], '1999')
        replayed = events(client.get(f'/playlist/enrich/events/{job_id}'))
        row = next(event for event in replayed if event['type'] == 'track' and event['video_id'] == 'quiet-0')
        assert row['genre'] == 'Jazz' and row['year'] == '1999'
        assert 'data-genre="jazz"' in row['row_html'] and 'data-year="1999"' in row['row_html']
        assert len(case.calls) == 1  # Reading/replaying never restarts the write.
        case.fault = None
        case.now += 3600  # Advance beyond the real persisted provider retry deadline.
        retry = client.post(start).json()['job_id']
        assert retry != job_id
        assert events(client.get(f'/playlist/enrich/events/{retry}'))[-1] == {'type': 'end', 'error': False}
        assert len(case.calls) == 2
        assert store.tracks.get_track_enrichment(case.tracks[0]) == ('Jazz', '1999')
        assert store.tracks.get_track_enrichment(case.tracks[1])[0] == 'Rock'


def test_album_retry_rejoins_reserved_worker_and_refresh_rejoins_stream(store, quiet_enrichment):
    case = quiet_enrichment
    case.fault = 'provider'
    case.release.clear()
    app = create_app(store, lambda: case.clients, now_fn=lambda: case.now)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        try:
            first = client.post('/album/QUIET-ALBUM/enrich').json()['job_id']
            assert case.observed.wait(3)
            second = client.post('/album/QUIET-ALBUM/enrich').json()['job_id']
            assert first == second and len(case.calls) == 1
            page = client.get('/album?browse=QUIET-ALBUM')
            assert page.context['active_job'].id == first
            assert f"enrichPanel(0, {first}, 'waterfall'" in page.text
            assert case.diagnostic not in page.text
        finally:
            case.release.set()
        assert client.get(f'/playlist/enrich/events/{first}').status_code == 200
        assert case.finished.wait(3)
        assert len(case.calls) == 1
        assert app.state.ctx.jobs.find_active_album('QUIET-ALBUM') is None


def test_album_reservation_is_complete_before_another_request_can_rejoin(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from yt_playlist.web import jobs

    assigning, release, second_entered = (threading.Event() for _ in range(3))
    original = jobs.SyncJob

    class PausedAlbumJob(original):
        def __setattr__(self, name, value):
            if name == 'album_browse' and value == 'BID' and not assigning.is_set():
                assigning.set()
                assert release.wait(5)
            super().__setattr__(name, value)

    monkeypatch.setattr(jobs, 'SyncJob', PausedAlbumJob)
    registry = jobs.SyncJobs()

    def second_request():
        second_entered.set()
        return registry.create_or_rejoin_album('BID')

    with ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(registry.create_or_rejoin_album, 'BID')
        try:
            assert assigning.wait(3)
            second = callers.submit(second_request)
            assert second_entered.wait(3)
            with pytest.raises(TimeoutError):
                second.result(timeout=.1)
        finally:
            release.set()
        (one, created), (two, joined) = first.result(timeout=3), second.result(timeout=3)
    assert one is two and created and not joined
    assert one.album_browse == 'BID' and one.source == 'waterfall'
    assert registry.find_active_album('BID') is one
    one.done = True
    assert registry.find_active_album('BID') is None
    replacement, created = registry.create_or_rejoin_album('BID')
    assert replacement is not one and created


def test_failed_album_worker_admission_releases_reservation(store, quiet_enrichment):
    from yt_playlist.core.activity import WorkClosing
    from yt_playlist.web.routes.album import build

    case = quiet_enrichment
    app = create_app(store, lambda: case.clients)
    ctx = app.state.ctx
    endpoint = next(route.endpoint for route in build(ctx).routes if route.path == '/album/{browse}/enrich')
    ctx.work.close_admission()
    with pytest.raises(WorkClosing):
        endpoint('QUIET-ALBUM')
    assert not ctx.jobs.has_active()
    assert ctx.jobs.get(1).done
    assert not case.calls
