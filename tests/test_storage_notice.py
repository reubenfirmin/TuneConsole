"""Short maintenance notices preserve the detailed API and write admission."""
import re

from fastapi.testclient import TestClient
import pytest

from yt_playlist.core import playback_feedback
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('preparation', ['recommendations', 'enrichment', 'retry'])
def test_preparation_uses_existing_notice_without_pausing_normal_use(store, monkeypatch, preparation):
    app = create_app(store, lambda: {}, background_work=False)
    ctx = app.state.ctx
    worker = ctx.rec_worker if preparation == 'recommendations' else ctx.enrich_worker
    if preparation == 'retry':
        from yt_playlist.rec.worker_queue import Job
        ctx.rec_worker.jobs.retry[Job.HOME].dirty = True
    else:
        monkeypatch.setattr(type(worker), 'busy', property(lambda _: True))
    with TestClient(app, base_url='http://127.0.0.1') as client:
        status = client.get('/storage-maintenance/status').json()
        assert status['banner_message'] == 'Preparing library data. Features will appear as they are ready.'
        assert not status['active'] and status['recording']
        html = client.get('/playlists').text
        assert html.count('id="storage-maintenance"') == 1
        assert status['banner_message'] in html
        # Last-good display output keeps working while analytics, refresh or
        # worker retries continue. Their activity alone is not a user notice.
        store.rec_proposals.put_proposals('home_prepared',
            {'schema': 1, 'protos': [{'label': 'Prepared mix', 'tracks': [{'videoId': 'prepared-track'}]}]}, 1)
        with monkeypatch.context() as prepared_only:
            def unexpected_decode(*args, **kwargs):
                raise AssertionError('The notice must only check prepared headers')
            prepared_only.setattr(store.rec_proposals, 'get_proposals', unexpected_decode)
            assert client.get('/storage-maintenance/status').json()['banner_message'] == ''
        prepared_html = client.get('/playlists').text
        assert re.search(r'<div id="storage-maintenance"[^>]* hidden>', prepared_html)
        store._recommendation_retirement_pending = True
        assert client.get('/storage-maintenance/status').json()['banner_message'] == (
            'Optimizing app storage; some recommendations are temporarily unavailable.')
        store._recommendation_retirement_pending = False
        ctx.rec_worker.jobs.cancel()
        monkeypatch.undo()
        assert client.get('/storage-maintenance/status').json()['banner_message'] == ''


@pytest.mark.parametrize('state,expected,active,warning', [
    ('idle', '', False, False),
    ('retirement', 'Optimizing app storage; some recommendations are temporarily unavailable.', True, False),
    ('evidence', '', False, False),
    ('retrying', '', False, True),
    ('feedback', '', False, False),
])
def test_notice_matches_status_without_javascript(store, state, expected, active, warning):
    app = create_app(store, lambda: {}, background_work=False)
    if state == 'retirement':
        store._recommendation_retirement_pending = True
    elif state in ('evidence', 'retrying'):
        store._evidence_recovery_pending = True
        store._evidence_recovery_failed = state == 'retrying'
    elif state == 'feedback':
        identity = store.accounts.upsert_account('Main', None, True)
        playback_feedback.capture(store, 'player', identity, {'kind': 'state', 'state': 'paused'}, 1)
    with TestClient(app, base_url='http://127.0.0.1') as client:
        status = client.get('/storage-maintenance/status').json()
        assert (status['active'], status['warning'], status['recording']) == (active, warning, not active)
        assert status['banner_message'] == expected
        html = client.get('/playlists').text
        notice = re.search(r'<div id="storage-maintenance"(.*?)>(.*?)</div>', html, re.S)
        assert notice is not None
        assert notice[2] == expected
        assert (' hidden' in notice[1]) == (not expected)
        assert 'role="status"' in notice[1]
        if expected:
            assert '\n' not in expected and expected.count('.') == 1
            assert status['message'] != expected
            assert status['message'] not in notice[2]
        if active:
            response = client.post('/sync')
            assert response.status_code == 503
            assert response.text == status['message']


def test_evidence_cleanup_stays_quiet_with_usable_recommendations(store):
    app = create_app(store,lambda:{},background_work=False)
    store._evidence_recovery_pending = True
    store.rec_proposals.put_proposals('home_prepared',{'schema':1,'protos':[
        {'label':'Available mix','tracks':[{'videoId':'prepared-track'}]}]},1)
    with TestClient(app,base_url='http://127.0.0.1') as client:
        status = client.get('/storage-maintenance/status').json()
        assert status['banner_message'] == ''
        assert not status['active'] and status['recording']
        assert re.search(r'<div id="storage-maintenance"[^>]* hidden>', client.get('/playlists').text)
        store._evidence_recovery_pending = False
        assert client.get('/storage-maintenance/status').json()['banner_message']==''

