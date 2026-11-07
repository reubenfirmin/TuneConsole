"""Bounded, same-origin browser evidence without collecting audio or annotations."""
import logging
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from yt_playlist.web.app import create_app


@pytest.mark.parametrize('enabled', ['0', '1'])
def test_audio_browser_diagnostics_log_capabilities_and_card_state(store, monkeypatch, caplog, enabled):
    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', enabled)
    value = {'page_id': 'diagnostic-document', 'asset_version': 'checked-asset',
        'event': 'cards', 'path': '/playlists', 'run_id': str(uuid4()),
        'card_count': 1, 'card_visible': True, 'can_label': True, 'samples': 0,
        'runtime_available': True, 'locks_available': True, 'secure_context': True}
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        with caplog.at_level(logging.INFO, logger='yt_playlist.web.routes.audio_vibes'):
            changes = store.conn.total_changes
            response = client.post('/audio-vibes/diagnostics', json=value)
            assert store.conn.total_changes == changes
        assert response.status_code == 200
        assert response.json() == {'recorded': True}
    records = [record for record in caplog.records if record.message.startswith('Audio vibe browser:')]
    assert len(records) == 1
    assert all(repr(item) in records[0].message for item in value.values())


def test_audio_browser_diagnostics_reject_unbounded_and_cross_origin_input(store, caplog):
    value = {'page_id': 'document', 'asset_version': 'asset', 'event': 'started', 'path': '/'}
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        for patch in ({'event': 'arbitrary-event'}, {'detail': 'x' * 257}, {'samples': 4},
                      {'run_id': 'invalid'}, {'card_visible': 'true'}, {'audio': [1, 2]}):
            assert client.post('/audio-vibes/diagnostics', json=value | patch).status_code == 422
        assert client.post('/audio-vibes/diagnostics', content=' ' * 65537).status_code == 413
        for origin in ('https://other.example', 'chrome-extension://opmmbolbdgdkphfocffpjpgmelkkjkcn'):
            assert client.post('/audio-vibes/diagnostics', json=value,
                               headers={'Origin': origin}).status_code == 403
    assert not any(record.message.startswith('Audio vibe browser:') for record in caplog.records)
