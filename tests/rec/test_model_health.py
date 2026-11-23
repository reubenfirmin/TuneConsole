"""Requests read bounded prepared diagnostics; the worker owns all fitting."""
import logging
from tests.enrichment_support import seed_audio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing
from yt_playlist.rec import eval_recs, model_health
from yt_playlist.rec.rec_worker import RecWorker
from yt_playlist.rec.worker_queue import Job
from yt_playlist.web.app import create_app


def test_cold_and_repeated_panel_requests_never_fit(store, monkeypatch):
    for name in ('recall_at_k', 'projection_recall', 'rolling_temporal_recall'):
        monkeypatch.setattr(eval_recs, name, lambda *a, **kw: pytest.fail('fit on request'))
    with TestClient(create_app(store, lambda: {}, background_work=False)) as client:
        for _ in range(3):
            response = client.get('/taste/recall')
            assert response.status_code == 200
            assert 'Preparing prediction checks in the background' in response.text
            assert 'Not enough history' not in response.text


def worker_for(store, monkeypatch):
    worker = RecWorker(SimpleNamespace(store=store, now_fn=lambda: 1000,
        client_provider=lambda: {}, logger=logging.getLogger(__name__)), debounce_s=0)
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    return worker


def test_health_requests_coalesce_and_refresh_only_after_input_changes(store, monkeypatch):
    worker = worker_for(store, monkeypatch)
    calls = []
    original = model_health.prepare

    def prepare(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(model_health, 'prepare', prepare)
    with ThreadPoolExecutor(max_workers=5) as requests:
        list(requests.map(lambda _: worker.trigger_health_refresh(), range(20)))
    assert worker.jobs.has(Job.MODEL_HEALTH)
    assert not worker.jobs.visible
    worker._run_pending()
    assert len(calls) == 1
    assert not model_health.view(store, 1000)['health_pending']
    worker.projections.retry_at.clear()
    worker.trigger_health_refresh()
    assert not worker.jobs.has(Job.MODEL_HEALTH)
    store.tracks.upsert_track('new', 'New song', 'Artist', None, 180)
    assert model_health.view(store, 1000)['health_pending']
    worker.trigger_health_refresh()
    worker._run_pending()
    assert len(calls) == 2
    assert not model_health.view(store, 1000)['health_pending']


@pytest.mark.parametrize('failure', ['budget', 'cancel', 'conflict'])
def test_failed_or_conflicting_refresh_preserves_last_good(store, monkeypatch, failure):
    previous = model_health.prepare(store, 1000)
    assert previous is not None

    def interrupted(*args, **kwargs):
        if failure == 'budget':
            raise WorkBudgetExceeded('bounded diagnostic')
        if failure == 'cancel':
            raise WorkClosing('shutdown')
        store.tracks.upsert_track('new', 'New song', 'Artist', None, 180)
        return previous['proj']

    monkeypatch.setattr(eval_recs, 'projection_recall', interrupted)
    if failure == 'conflict':
        assert model_health.prepare(store, 1001) is None
    else:
        with pytest.raises(WorkBudgetExceeded if failure == 'budget' else WorkClosing):
            model_health.prepare(store, 1001)
    assert store.rec_proposals.get_proposals(model_health.SURFACE) == previous


def test_worker_failure_retries_with_backoff(store, monkeypatch):
    worker = worker_for(store, monkeypatch)
    previous = model_health.prepare(store, 1000)
    store.tracks.upsert_track('new', 'New song', 'Artist', None, 180)
    monkeypatch.setattr(model_health, 'prepare', lambda *a: None)
    worker.trigger_health_refresh()
    worker._run_pending()
    assert worker.projections.failures['model_health'] == 1
    assert worker.health_refresh_failed
    failed_view = model_health.view(store, 1000, failed=worker.health_refresh_failed)
    assert failed_view['health_ready'] and failed_view['health_pending'] and failed_view['health_failed']
    worker.trigger_health_refresh()
    assert not worker.jobs.has(Job.MODEL_HEALTH)
    assert store.rec_proposals.get_proposals(model_health.SURFACE) == previous


def test_health_generation_tracks_enrichment_listening_and_model_settings(store):
    iid = store.accounts.upsert_account('main', None, True)
    track = store.tracks.upsert_track('v', 'Song', 'Artist', None, 180)
    mutations = [
        lambda: seed_audio(store, track, danceability=0.8),
        lambda: store.history.record_history_plays(iid, 1000, ['song'], source_timezone='UTC'),
        lambda: store.settings.set_setting('rec_cooc_weighting', '1'),
        lambda: store.rec_model.replace_rec_vectors([('song|artist', b'\0\0\0\0')]),
    ]
    for mutate in mutations:
        before = model_health.input_key(store, 1000)
        mutate()
        assert model_health.input_key(store, 1000) != before
    before = model_health.input_key(store, 1000)
    store.player_events.record_player_event(iid, 'progress', 'v', 30, 200, None, '{}', 1000)
    assert model_health.input_key(store, 1000) == before


def test_future_listening_excludes_date_only_plays_at_window_boundaries(store, monkeypatch):
    day = 86400
    cutoff = datetime(2025, 11, 1, 18, tzinfo=timezone.utc).timestamp()
    end = cutoff + day
    account = store.accounts.upsert_account('main', None, True)
    titles = ['Earlier exact', 'Later exact', 'Cutoff date', 'Prior date',
              'Second earlier exact', 'Latest exact', 'End date']
    titles.extend(f'Unplayed song {i}' for i in range(53))
    tracks = [store.tracks.upsert_track(str(i), title, 'Artist', None, 180,
                                       created_at=cutoff - 4 * day)
              for i, title in enumerate(titles)]
    keys = [store.tracks.identity_key_for_video(str(i)) for i in range(len(tracks))]
    for start in range(0, len(tracks), 12):
        playlist = store.playlists.upsert_playlist(account, f'playlist-{start}', f'List {start}',
                                                   12, 'unchanged', cutoff - 3 * day)
        store.playlists.set_playlist_tracks(playlist, tracks[start:start + 12])
    store.settings.set_setting('rec_cooc_weighting', '1')
    for i, at in ((0, cutoff - 120), (1, cutoff + 120), (4, cutoff - 180), (5, end)):
        store.history.record_play_event(account, keys[i], str(i), at, source_timezone='UTC')
    for i, observed_at in ((2, cutoff + 7200), (3, cutoff - day), (6, end + 7200)):
        store.history.record_history_plays(account, observed_at, [(keys[i], 'Today')],
                                           source_timezone='UTC')

    windows = []
    score = eval_recs.causal_recall

    def capture(**window):
        windows.append(window)
        return score(**window)

    monkeypatch.setattr(eval_recs, 'causal_recall', capture)
    now = end + 3 * 3600
    worker = worker_for(store, monkeypatch)
    worker.ctx.now_fn = lambda: now
    worker.trigger_health_refresh()
    worker._run_pending()

    window = windows[0]
    assert window['cutoff'] == cutoff + 1e-6
    assert window['end'] == end + 1e-6
    assert window['before'] == {keys[0], keys[3], keys[4]}
    assert set(window['weights']) == window['before']
    assert {keys[0], keys[4]} in [set(basket) for basket in window['baskets']]
    assert window['after'] == {keys[1], keys[5]}
    report = model_health.view(store, now)
    assert report['health_ready'] and report['temporal']['causal']
    assert report['temporal']['windows'][0]['trials'] == 2


def test_new_embedding_publication_invalidates_report_and_rejects_stale_refresh(store, monkeypatch):
    import numpy as np

    store.tracks.upsert_track('v', 'Song', 'Artist', None, 180)
    key = store.tracks.identity_key_for_video('v')
    store.rec_model.replace_rec_vectors([(key, np.array([1., 0.], dtype=np.float32).tobytes())])
    previous = model_health.prepare(store, 1000)
    assert previous is not None and not model_health.view(store, 1000)['health_pending']

    store.rec_model.replace_rec_vectors([(key, np.array([0., 1.], dtype=np.float32).tobytes())])
    assert model_health.view(store, 1000)['health_pending']
    assert store.rec_proposals.get_proposals(model_health.SURFACE) == previous

    def replace_while_fitting(*args, **kwargs):
        store.rec_model.replace_rec_vectors([(key, np.array([.5, .5], dtype=np.float32).tobytes())])
        return previous['proj']

    with monkeypatch.context() as patch:
        patch.setattr(eval_recs, 'projection_recall', replace_while_fitting)
        assert model_health.prepare(store, 1001) is None
    assert store.rec_proposals.get_proposals(model_health.SURFACE) == previous
    assert model_health.view(store, 1001)['health_pending']

    refreshed = model_health.prepare(store, 1002)
    assert refreshed is not None and refreshed['inputs'] != previous['inputs']
    assert not model_health.view(store, 1002)['health_pending']
