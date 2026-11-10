"""An import lock transfers to a worker only after successful scheduling."""
import threading

from fastapi.testclient import TestClient
import pytest

from tests.test_spotify_album_import import AlbumClient, album_zip
from tests.web.test_spotify_data_route import _prepare_library, _select_library, _wait_for_library_job
from yt_playlist.web.app import create_app
from yt_playlist.web.routes import setup
from yt_playlist.web.spotify_import import current_job


@pytest.mark.parametrize('phase', ['prepare', 'confirm'])
@pytest.mark.parametrize('failure', ['persistence', 'thread-create', 'thread-start', 'provider'])
def test_failed_start_releases_import_for_recovery_and_retry(store, monkeypatch, phase, failure):
    iid = store.accounts.upsert_account('Main', 'bridge', None, True)
    app = create_app(store, lambda: {iid: AlbumClient()}, now_fn=lambda: 1000, background_work=False)
    ctx = app.state.ctx
    web = TestClient(app, base_url='http://127.0.0.1:8123')
    if phase == 'prepare':
        token, _ = _select_library(web, album_zip('One'))
        data = {'selection_token': token, 'identity_id': iid, 'include_albums': 'on'}
    else:
        token = _prepare_library(web, store, iid, payload=album_zip('One'), indices=())
        data = {'preview_token': token}

    def fail(*args, **kwargs):
        raise RuntimeError('Controlled pre-worker failure')

    with monkeypatch.context() as broken:
        if failure == 'persistence':
            broken.setattr(setup, 'save_job', fail)
        elif failure == 'provider':
            broken.setattr(setup, 'prepare_spotify_library' if phase == 'prepare' else 'import_spotify_library', fail)
        elif failure == 'thread-create':
            construct = threading.Thread
            def failed_constructor(*args, **kwargs):
                if kwargs.get('name') in {'spotify-library-preview', 'spotify-library-import'}:
                    fail()
                return construct(*args, **kwargs)
            broken.setattr(threading, 'Thread', failed_constructor)
        else:
            start = threading.Thread.start
            def failed_start(thread):
                if thread.name in {'spotify-library-preview', 'spotify-library-import'}:
                    fail()
                return start(thread)
            broken.setattr(threading.Thread, 'start', failed_start)
        if failure == 'provider':
            assert web.post('/import/spotify-library/' + phase, data=data,
                            follow_redirects=False).status_code in {200, 303}
            assert _wait_for_library_job(web)['status'] == 'error'
            assert ctx.work.wait(2)
        else:
            with pytest.raises(RuntimeError, match='Controlled pre-worker failure'):
                web.post('/import/spotify-library/' + phase, data=data, follow_redirects=False)
    assert not ctx.spotify_import_lock.locked()
    assert ctx.work.wait(.5)
    recovered = current_job(ctx)
    if failure in {'thread-create', 'thread-start', 'provider'}:
        assert recovered['status'] == 'error'
    # The consumed token is still single-use; recovery needs a new preview.
    assert web.post('/import/spotify-library/' + phase, data=data).status_code == 410
    assert web.post('/import/spotify-library/delete-report', follow_redirects=False).status_code == 303
    preview = _prepare_library(web, store, iid, payload=album_zip('One'), indices=())
    assert web.post('/import/spotify-library/confirm', data={'preview_token': preview},
                    follow_redirects=False).status_code == 303
    assert _wait_for_library_job(web)['status'] == 'done'
    assert ctx.work.wait(2)
    assert not ctx.spotify_import_lock.locked()
    assert web.post('/import/spotify-library/delete-report', follow_redirects=False).status_code == 303
