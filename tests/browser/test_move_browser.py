"""An uncertain move preserves its copy and reconciles without replaying deletion."""
import json
from pathlib import Path
import socket

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.browser.quiet_action_support import action_evidence, assert_quiet_action
from tests.conftest import FakeClient, _track
from tests.live_server import LiveServer
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


@pytest.fixture
def live_move_app(store):
    i1 = store.accounts.upsert_account("Main", "c1", None, True)
    i2 = store.accounts.upsert_account("Alt", "c2", None, False)
    tracks = {}
    for n, title in enumerate(("Evening", "Mix", "Weekend")):
        pid = store.playlists.upsert_playlist(i1, f"PL{n}", title, 1, "h", 1.0)
        track = store.tracks.upsert_track(f"v{n}", f"Song {n}", "Artist", None, None, 1)
        store.playlists.set_playlist_tracks(pid, [track])
        tracks[f"PL{n}"] = [_track(f"v{n}", f"Song {n}", "Artist")]
    store.playlists.upsert_playlist(i2, "PL_ALT", "Already here", 0, "h", 1.0)
    clients = {i1: FakeClient(tracks=tracks),
               i2: FakeClient(catalog={t[0]["videoId"]: t[0] for t in tracks.values()})}
    app = create_app(store, lambda: clients, now_fn=lambda: 1.0)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        with LiveServer(server, sockets=[sock]):
            yield {"base": f"http://127.0.0.1:{port}", "i1": i1, "i2": i2, "clients": clients}


@pytest.mark.parametrize('remote_deleted', [False])
def test_uncertain_move_keeps_copy_and_rechecks_without_repeating_deletion(
        live_move_app, store, page, monkeypatch, remote_deleted, action_evidence):
    app = live_move_app
    source, target = app['clients'][app['i1']], app['clients'][app['i2']]
    original = next(p.id for p in store.playlists.get_playlists()
                    if p.account_id == app['i1'] and p.provider_playlist_id == 'PL1')
    attempts = []
    diagnostic = 'sqlite3.OperationalError: internal-diagnostic-879-move'

    def uncertain_delete(playlist):
        attempts.append(playlist)
        raise RuntimeError(diagnostic)

    monkeypatch.setattr(source, 'delete_playlist', uncertain_delete)
    page.goto(app['base'] + '/move')
    page.get_by_role('checkbox', name='Select Mix', exact=True).check()
    page.get_by_role('button', name='Move', exact=True).click()
    expect(page.get_by_text('Copy created. Source deletion needs review in Actions.', exact=True)).to_be_visible()
    expect(page.get_by_text('Source kept', exact=False)).to_have_count(0)
    expect(page.get_by_role('link', name='Mix', exact=True)).to_be_visible()
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-move-uncertain.png')
    action, = store.actions.get_actions()
    assert action.status == 'delete_pending'
    entry, = json.loads(action.undo_json)['deletions']
    assert entry['phase'] == 'uncertain'
    backup = Path(entry['backup']).read_bytes()
    assert len(target.created) == len(target.added) == 1 and attempts == ['PL1']
    destination, = [p for p in store.playlists.get_playlists()
                    if p.account_id == app['i2'] and p.provider_playlist_id == target.created[0][0]]
    assert destination.track_count == 1
    page.reload()
    expect(page.get_by_role('link', name='Mix', exact=True)).to_be_visible()
    assert_quiet_action(page)
    assert json.loads(store.actions.get_action(action.id).undo_json)['deletions'] == [entry]
    page.goto(app['base'] + '/actions')
    row = page.get_by_role('row').filter(has=page.get_by_role('cell', name=str(action.id), exact=True))
    expect(row.get_by_role('button', name='Recheck deletion')).to_be_visible()
    expect(page.locator('body')).not_to_contain_text(diagnostic)
    page.screenshot(path=action_evidence + '-move-action-reload.png')
    row.get_by_role('button', name='Recheck deletion').click()
    expect(row.locator('td').nth(3)).to_have_text('deletion failed')
    final = store.actions.get_action(action.id)
    assert final.status == 'delete_failed'
    assert store.playlists.get_playlist(original) is not None
    assert len(target.created) == len(target.added) == 1 and attempts == ['PL1']
    assert Path(entry['backup']).read_bytes() == backup
    expect(page.locator('body')).not_to_contain_text(diagnostic)
    expect(page.locator('#navigation-error, .toast-err, .notice.err.banner:visible')).to_have_count(0)
    expect(page.locator('.notice.ok.banner')).to_contain_text(
        'The playlist still exists. No deletion was retried; the backup is saved.')
    page.screenshot(path=action_evidence + '-move-reconciled.png')
