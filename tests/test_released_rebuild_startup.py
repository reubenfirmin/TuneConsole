"""Released libraries stay available without running populated migrations."""
from fastapi.testclient import TestClient
import pytest

from tests.conftest import FakeClient
from tests.db_rebuild_support import production64
from yt_playlist.core import playback_feedback
from yt_playlist.core.runtime import Runtime
from yt_playlist.core.setup import write_config
from yt_playlist.core.store import Store
from yt_playlist.core.db_space import StorageCompacting
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('background', [False, True])
def test_released_source_serves_playlists_without_advancing_history(production64, background):
    before = production64.read_bytes()
    store = Store(production64)
    try:
        store.init_schema()
        assert store.activity_paused
        assert store.conn.execute('PRAGMA query_only').fetchone()[0] == 1
        assert store.conn.total_changes == 0
        assert [p.id for p in store.playlists.get_playlists()] == [41, 73]
        with pytest.raises(StorageCompacting):
            store.settings.set_setting('forbidden_write', '1')
        remote = FakeClient()
        app = create_app(store, lambda: {11: remote, 29: FakeClient()}, background_work=background)
        with TestClient(app, base_url='http://127.0.0.1') as client:
            assert client.get('/').status_code == 200
            assert client.get('/playlist/41').status_code == 200
            status = client.get('/storage-maintenance/status').json()
            assert status['active'] and not status['recording']
            response = client.post('/track/like', data={'video_id': 'video-3', 'on': '1'})
            assert response.status_code == 200
            assert remote.rated == [('video-3', 'LIKE')]
            assert playback_feedback.inbox(store).pending() == 1
            assert playback_feedback.drain(store) == 0
            assert client.post('/sync').status_code == 503
        assert store.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 64
    finally:
        store.conn.close()
    assert production64.read_bytes() == before


def test_config_load_uses_existing_account_ids_without_reconciling(production64, tmp_path, monkeypatch):
    import yt_playlist.core.runtime as runtime
    monkeypatch.setattr(runtime, 'build_client', lambda *args: object())
    config = tmp_path/'config.toml'
    write_config([{'id': 11, 'label': 'Main', 'credential_ref': 'main.json', 'is_master': True},
                  {'id': 29, 'label': 'Other account', 'credential_ref': 'other.json'}], config)
    store = Store(production64)
    try:
        store.init_schema()
        rt = Runtime(store, config, tmp_path)
        rt.load()
        assert rt.configured and set(rt.clients()) == {11, 29}
        assert store.conn.total_changes == 0
    finally:
        store.conn.close()
