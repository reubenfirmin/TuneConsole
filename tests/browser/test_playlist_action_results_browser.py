"""Real HTMX navigation preserves failed and partial bulk-action results."""
import pytest
from playwright.sync_api import expect

from yt_playlist.library.ops import PlaylistOps
from tests.browser.quiet_action_support import action_evidence, assert_quiet_action


@pytest.fixture
def undo_app(store):
    import socket
    import uvicorn
    from tests.live_server import LiveServer
    from tests.test_undo_recovery import RecoveryClient, _deleted
    from yt_playlist.web.app import create_app

    provider = RecoveryClient({'SOURCE': ['a', 'b']})
    iid, aid = _deleted(store, provider)
    app = create_app(store, lambda: {iid: provider})
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    with LiveServer(server):
        yield f'http://127.0.0.1:{port}', provider, aid


def test_undo_failure_and_reload_preserve_journal_without_repeating_completed_writes(
        undo_app, store, page, monkeypatch, action_evidence, caplog):
    import json
    from yt_playlist.core import fao
    from yt_playlist.library import playlist_undo

    base, provider, aid = undo_app
    diagnostic = 'sqlite3.OperationalError: Undo journal corrupt [review-879-undo-browser]'
    backup = json.loads(store.actions.get_action(aid).undo_json)['backup']
    original_backup = fao.read(backup).bytes
    refresh = playlist_undo._refresh

    def fail_refresh(*_args, **_kwargs):
        raise RuntimeError(diagnostic)

    monkeypatch.setattr(playlist_undo, '_refresh', fail_refresh)
    page.goto(base + '/actions')
    row = page.get_by_role('row').filter(has=page.get_by_role('cell', name=str(aid), exact=True))
    row.get_by_role('button', name='Undo', exact=True).click()
    expect(row.get_by_role('button', name='Resume Undo', exact=True)).to_be_visible()
    expect(row).to_contain_text('recovery incomplete')
    expect(row).to_contain_text('Progress is saved')
    recovery = json.loads(store.actions.get_action(aid).undo_json)['recovery']
    assert recovery['error'] == diagnostic
    step, = recovery['steps']
    assert step['phase'] == 'remote_complete' and step['created_id'] == 'PL_NEW_0'
    assert diagnostic in caplog.text
    writes = (list(provider.created), list(provider.added), list(provider.deleted))
    assert len(provider.created) == len(provider.added) == 1
    for suffix in ('failed', 'reloaded'):
        if suffix == 'reloaded':
            page.reload()
        expect(row).not_to_contain_text(diagnostic)
        expect(row.locator('a[href*="PL_NEW_0"]')).to_be_visible()
        expect(row.get_by_role('button', name='Resume Undo')).to_be_enabled()
        assert_quiet_action(page)
        expect(page.locator('.notice.banner')).to_have_count(0)
        page.screenshot(path=action_evidence + '-undo-' + suffix + '.png')
        assert json.loads(store.actions.get_action(aid).undo_json)['recovery'] == recovery
        assert fao.read(backup).bytes == original_backup
        assert (provider.created, provider.added, provider.deleted) == writes
    monkeypatch.setattr(playlist_undo, '_refresh', refresh)
    row.get_by_role('button', name='Resume Undo').click()
    expect(row.locator('td').nth(3)).to_have_text('undone')
    assert store.actions.get_action(aid).status == 'undone'
    assert (provider.created, provider.added, provider.deleted) == writes
    assert fao.read(backup).bytes == original_backup
    page.screenshot(path=action_evidence + '-undo-resumed.png')


@pytest.mark.parametrize("operation", ["copy", "delete"])
@pytest.mark.parametrize("outcome", ["failed", "partial"])
def test_bulk_action_result_survives_navigation_and_reload(live_app, store, page, monkeypatch, operation, outcome):
    first = store.playlists.get_playlists()[0]
    second = store.playlists.upsert_playlist(first.account_id, "PLB", "Second playlist", 0, "", 0)
    calls = []
    if operation == "copy":
        def copy(*args):
            calls.append("copy")
            if outcome == "failed":
                raise RuntimeError("transport-failure-879: database connection lost")
            return {"new_ytm": "NEW", "title": "My copy", "added": 2, "skipped": 1,
                    "skipped_video_ids": ["missing"]}
        monkeypatch.setattr(PlaylistOps, "copy", copy)
    else:
        def delete(self, pid):
            calls.append(pid)
            if outcome == "failed" or pid == first.id:
                raise ValueError("transport-failure-879: provider DELETE timeout")
            store.playlists.remove_playlist(pid)
        monkeypatch.setattr(PlaylistOps, "delete", delete)
    page.goto(f"{live_app}/playlists")
    page.locator(f'.playlist-table-card tr[data-playlist-id="{first.id}"] input').check()
    if operation == "copy":
        page.get_by_role("button", name="Copy", exact=True).click()
        page.get_by_placeholder("New playlist name").fill("My copy")
        page.get_by_placeholder("New playlist name").press("Enter")
    else:
        page.locator(f'.playlist-table-card tr[data-playlist-id="{second}"] input').check()
        page.get_by_role("button", name="Delete", exact=True).click()
        page.get_by_role("button", name="Delete them", exact=True).click()
    result = page.get_by_role("group", name="Playlist action result")
    expect(result).to_be_visible()
    expect(result).to_have_attribute("data-outcome", outcome)
    expect(result).to_contain_text("Copy not confirmed" if operation == "copy" and outcome == "failed" else
                                  "Not confirmed" if operation == "delete" else "Copied 2 track(s)")
    expect(page.locator('.playlist-head-actions [aria-label="Playlist action result"]')).to_be_visible()
    expect(page.locator('body')).not_to_contain_text('transport-failure-879')
    expect(page.get_by_role("alert", name="Playlist action result")).to_have_count(0)
    retained = operation == "delete" or outcome == "failed"
    first_checkbox = page.locator(f'.playlist-table-card tr[data-playlist-id="{first.id}"] input')
    if retained:
        expect(page.locator(".pl-actionbar")).to_be_visible()
        expect(first_checkbox).to_be_checked()
    else:
        expect(page.locator(".pl-actionbar")).to_be_hidden()
    if operation == "delete" and outcome == "partial":
        expect(page.locator(f'.playlist-table-card tr[data-playlist-id="{second}"]')).to_have_count(0)
        expect(result).to_contain_text('Second playlist')
    count = len(calls)
    page.reload()
    expect(result).to_be_visible()
    expect(page.locator('body')).not_to_contain_text('transport-failure-879')
    if retained:
        expect(first_checkbox).to_be_checked()
    if operation == "copy" and outcome == "failed":
        page.get_by_role("button", name="Copy", exact=True).click()
        expect(page.get_by_placeholder("New playlist name")).to_have_value("My copy")
    assert len(calls) == count
