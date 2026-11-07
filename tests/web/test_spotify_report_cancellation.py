"""Cancelled request awaiters cannot abandon album report work."""
import asyncio
import threading

from fastapi.testclient import TestClient
import httpx
import pytest

from tests.test_spotify_album_import import AlbumClient, album_zip
from tests.web.test_spotify_data_route import _select_library
from yt_playlist.library.spotify_library import import_spotify_library, SPOTIFY_REPORT_SCHEMA
from yt_playlist.web.app import create_app
from yt_playlist.web.spotify_import import JOB_KEY, load_job, save_job


@pytest.mark.parametrize('operation', ['find-album-candidates', 'resolve-album'])
@pytest.mark.parametrize('failed', [False, True], ids=['success', 'provider-failure'])
def test_cancelled_album_request_keeps_owner_through_publication(store, operation, failed):
    iid = store.accounts.upsert_account('Main', 'bridge', None, True)
    entered, release = threading.Event(), threading.Event()

    class HeldClient(AlbumClient):
        blocked = False

        def hold(self):
            entered.set()
            assert release.wait(5), 'The test did not release the provider'
            if failed:
                raise RuntimeError('Controlled provider failure after cancellation')

        def search(self, query, filter='songs'):
            if self.blocked and operation == 'find-album-candidates':
                self.hold()
            return super().search(query, filter)

        def rate_playlist(self, playlist_id, rating):
            if self.blocked and operation == 'resolve-album':
                self.hold()
            return super().rate_playlist(playlist_id, rating)

    remote = HeldClient()
    remote.results['One Artist'].append({**remote.results['One Artist'][0], 'browseId': 'MPRE-alternate'})
    report = import_spotify_library(store, album_zip('One'), remote, iid, 100)
    assert report['albums_unmatched'] == 1
    save_job(store, {'status': 'done', 'account_id': iid,
                     'report_schema': SPOTIFY_REPORT_SCHEMA, 'report': report})
    app = create_app(store, lambda: {iid: remote}, now_fn=lambda: 1000, background_work=False)
    ctx = app.state.ctx
    selection, _ = _select_library(TestClient(app, base_url='http://127.0.0.1:8123'), album_zip('Two'))
    baseline = store.settings.get_setting(JOB_KEY)
    remote.blocked = True

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url='http://127.0.0.1:8123') as web:
            request = asyncio.create_task(web.post('/import/spotify-library/' + operation,
                                                   data={'album_index': '0', 'browse_id': 'MPRE-One'}))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert ctx.spotify_import_lock.locked() and ctx.work.active
                for path, data in (
                    ('delete-report', {}),
                    ('prepare', {'selection_token': selection, 'identity_id': iid, 'include_albums': 'on'}),
                    ('resolve-album', {'album_index': '0', 'browse_id': 'MPRE-One'}),
                ):
                    response = await asyncio.wait_for(web.post('/import/spotify-library/' + path, data=data), 2)
                    assert response.status_code == 409
                assert store.settings.get_setting(JOB_KEY) == baseline
                ctx.work.close_admission()
                assert not ctx.work.wait(.01)
            finally:
                release.set()
                if not request.done():
                    request.cancel()
                assert await asyncio.to_thread(ctx.work.wait, 3)
            assert not ctx.spotify_import_lock.locked()
            ctx.work.open()
            latest = load_job(store)['report']
            if operation == 'resolve-album' and not failed:
                assert latest['albums_unmatched'] == 0 and latest['albums_imported'] == 1
                assert latest['details']['albums'][0]['status'] == 'imported'
                assert remote.album_ratings == [('OLAK-One', 'LIKE')]
                assert len(store.collection.get_saved_albums(account_id=iid)) == 1
                assert store.settings.get_setting('spotify_library_imported_at') == '1000'
            else:
                assert latest['albums_unmatched'] == 1 and latest['albums_imported'] == 0
                assert remote.album_ratings == []
                assert store.settings.get_setting('spotify_library_imported_at') is None
                if not failed:
                    assert latest['details']['albums'][0]['candidates']
                else:
                    assert store.settings.get_setting(JOB_KEY) == baseline
            assert (await web.post('/import/spotify-library/delete-report')).status_code == 303

    asyncio.run(run())
