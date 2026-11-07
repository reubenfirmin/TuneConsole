"""Independent checks for diagnostics in the real alternate-version fragment."""
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from yt_playlist.web.app import create_app


@pytest.mark.parametrize('fault', ['storage_value', 'malformed_provider'])
def test_alternate_failure_keeps_diagnostics_internal_and_preserves_playlist(
        store, monkeypatch, fault, caplog):
    diagnostic = 'internal-diagnostic-879-alternates: invalid stored account document'
    identity = store.accounts.upsert_account('Main', 'credential', None, True)
    playlist = store.playlists.upsert_playlist(identity, 'QUIET-ALTERNATES', 'Keep this mix', 1, '', 1)
    track = store.tracks.upsert_track('quiet-original', 'Original song', 'Artist', 'Album', 200, 1)
    store.playlists.set_playlist_tracks(playlist, [track])
    original_rows = store.charts.playlist_tracks_detail(playlist)

    class Provider(FakeClient):
        def __init__(self):
            super().__init__()
            self.searches = []

        def search(self, query, filter='songs'):
            self.searches.append((query, filter))
            return super().search(query, filter=filter)

    provider = Provider()
    app = create_app(store, lambda: {identity: provider}, now_fn=lambda: 1000)
    url = f'/playlist/{playlist}/alternates?video_id=quiet-original'
    with TestClient(app, base_url='http://127.0.0.1') as client:
        with monkeypatch.context() as patch:
            if fault == 'storage_value':
                def failed_lookup(*args, **kwargs):
                    raise ValueError(diagnostic)
                patch.setattr(store.playlists, 'get_playlist', failed_lookup)
            else:
                # This fails in real result parsing, after the provider request
                # boundary that already catches transport/decode exceptions.
                provider._search_results = [None]
            response = client.get(url)

        assert diagnostic not in response.text
        assert 'YouTube returned an unexpected response' not in response.text
        assert 'hx-swap-oob="afterbegin:#toasts"' not in response.text
        if fault == 'storage_value':
            assert diagnostic in caplog.text
        else:
            assert 'alternate' in caplog.text.lower()
        assert len(provider.searches) == (0 if fault == 'storage_value' else 1)
        assert store.charts.playlist_tracks_detail(playlist) == original_rows
        assert not provider.created and not provider.added and not provider.deleted
        assert not provider.edited and not provider.rated

        # Explicitly reopening the read can recover without changing the mix.
        provider._search_results = [{'videoId': 'quiet-live', 'title': 'Original song (Live)',
                                     'artists': [{'name': 'Artist'}], 'duration_seconds': 240}]
        recovered = client.get(url)
        assert recovered.status_code == 200 and 'Original song (Live)' in recovered.text
        assert len(provider.searches) == (0 if fault == 'storage_value' else 1) + 3
        assert store.charts.playlist_tracks_detail(playlist) == original_rows
        assert not provider.created and not provider.added and not provider.deleted
        assert not provider.edited and not provider.rated
