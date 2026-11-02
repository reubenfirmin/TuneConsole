"""Short maintenance notices preserve the detailed API and write admission."""
import re

from fastapi.testclient import TestClient
import pytest

from yt_playlist.core import db_space, playback_feedback
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('preparation', ['recommendations', 'enrichment', 'maintenance', 'retry'])
def test_preparation_uses_existing_notice_without_pausing_normal_use(store, monkeypatch, preparation):
    app = create_app(store, lambda: {}, background_work=False)
    ctx = app.state.ctx
    worker = ctx.rec_worker if preparation == 'recommendations' else ctx.enrich_worker
    if preparation == 'retry':
        from yt_playlist.rec.worker_queue import Job
        ctx.rec_worker.jobs.retry[Job.HOME].dirty = True
    elif preparation == 'maintenance':
        ctx.storage_maintenance.ready.clear()
    else:
        monkeypatch.setattr(type(worker), 'busy', property(lambda _: True))
    with TestClient(app, base_url='http://127.0.0.1') as client:
        status = client.get('/storage-maintenance/status').json()
        assert status['banner_message'] == 'Preparing library data. Features will appear as they are ready.'
        assert not status['active'] and status['recording']
        html = client.get('/playlists').text
        assert html.count('id="storage-maintenance"') == 1
        assert status['banner_message'] in html
        ctx.storage_maintenance.active = True
        assert client.get('/storage-maintenance/status').json()['banner_message'] == (
            'Performing database maintenance - this should take just a few minutes.')
        ctx.storage_maintenance.active = False
        ctx.storage_maintenance.ready.set()
        ctx.rec_worker.jobs.cancel()
        monkeypatch.undo()
        assert client.get('/storage-maintenance/status').json()['banner_message'] == ''


@pytest.mark.parametrize('state,expected,active,warning', [
    ('idle', '', False, False),
    ('compacting', 'Performing database maintenance - this should take just a few minutes.', True, False),
    ('evidence', 'Performing database maintenance - this should take just a few minutes.', True, False),
    ('retrying', 'Library updates need attention.', True, True),
    ('feedback', 'Catching up on your listening.', True, False),
    ('low_space', 'Free disk space to finish.', False, True),
    ('deferred', 'Library update will retry.', False, True),
    ('schema_retry', 'Library update needs a retry.', False, True),
])
def test_notice_matches_status_without_javascript(store, state, expected, active, warning):
    app = create_app(store, lambda: {}, background_work=False)
    maintenance = app.state.ctx.storage_maintenance
    if state == 'compacting':
        maintenance.active = True
    elif state in ('evidence', 'retrying'):
        store._evidence_recovery_pending = True
        store._evidence_recovery_failed = state == 'retrying'
    elif state == 'feedback':
        identity = store.accounts.upsert_account('Main', 'credential', None, True)
        playback_feedback.capture(store, 'player', identity, {'kind': 'state', 'state': 'paused'}, 1)
    elif state in ('low_space', 'deferred'):
        maintenance.phase = 'deferred'
        maintenance._deferred(db_space.LowDiskSpace('full') if state == 'low_space' else RuntimeError('stopped'))
    elif state == 'schema_retry':
        maintenance.phase = state
    with TestClient(app, base_url='http://127.0.0.1') as client:
        status = client.get('/storage-maintenance/status').json()
        assert (status['active'], status['warning'], status['recording']) == (active, warning, not active)
        assert status['banner_message'] == expected
        html = client.get('/playlists').text
        notice = re.search(r'<div id="storage-maintenance"(.*?)>(.*?)</div>', html, re.S)
        assert notice is not None
        assert notice[2] == expected
        assert (' hidden' in notice[1]) == (not expected)
        assert f'role="{"alert" if warning else "status"}"' in notice[1]
        if expected:
            assert '\n' not in expected and expected.count('.') == 1
            assert status['message'] != expected
            assert status['message'] not in notice[2]
        if active:
            response = client.post('/sync')
            assert response.status_code == 503
            assert response.text == status['message']

