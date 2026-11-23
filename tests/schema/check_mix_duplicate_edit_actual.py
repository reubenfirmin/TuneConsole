"""Exercise the reported Mix Tapes selection on a private library snapshot."""
from contextlib import closing
import json
from time import perf_counter

from fastapi.testclient import TestClient

from tests.conftest import FakeClient
from tests.library_image.fixtures import managed_database, report_file
from tests.schema.check_mix_launch_actual import OfferedMixes
from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app


def test_lift_my_mood_keeps_one_copy_of_the_shamen_edit(managed_library, monkeypatch):
    report = {}
    managed_database(report=report)
    now = report['managed_library']['created_at']
    edits = {'PCTBdcuyB1I', 'FnT1z2MxPjQ'}
    store = Store(managed_library)
    try:
        with closing(store.conn):
            store.init_schema()
            sources = [dict(row) for row in store.conn.execute(
                'SELECT identity_key,video_id,orig_title,artist_browse_id,duration_s FROM catalog_tracks '
                'WHERE video_id IN (?,?) ORDER BY video_id', sorted(edits))]
            report['sources'] = sources
            assert len({row['identity_key'] for row in sources}) == 2
            assert {(row['orig_title'], row['artist_browse_id'], row['duration_s']) for row in sources} == {
                ('Ebeneezer Goode (Beat Edit)', 'UClLNGF-6oP0p33c2x_K8VjA', 233)}
            account = store.accounts.get_master_account().id
            provider = FakeClient()
            monkeypatch.setattr('yt_playlist.core.updatecheck._raw_version', lambda: '0.0.0.dev0')
            app = create_app(store, lambda: {account: provider}, now_fn=lambda: now, background_work=False)
            with TestClient(app, base_url='http://127.0.0.1') as client:
                assert client.get('/').status_code == 200
                started = perf_counter()
                response = client.post('/home/vibe', data={'vibe': 'uplift'})
                report['vibe_request_seconds'] = round(perf_counter() - started, 3)
                assert response.status_code == 200, response.text[:500]
                assert 'data-vibe="uplift"' in response.text
                card = next(card for card in OfferedMixes(response.text).cards if card['id'] == 'gen-wheelhouse')
                videos = [track['video_id'] for track in card['tracks']]
                report['wheelhouse'] = {'tracks': card['tracks'], 'recipe': json.loads(card['recipe'])}
                assert len(videos) == len(set(videos)) == 12, videos
                assert len(edits.intersection(videos)) == 1, videos
    finally:
        report_file('/tmp/tuneconsole-1560-mix-edit.json').write_text(json.dumps(report, indent=2) + '\n')
