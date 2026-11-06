"""Live-behavior tests for the server-side merge editor: each control posts to /merge/update
(which mutates the in-memory draft) and re-renders #merge-body; Apply -> HX-Redirect."""
import socket

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, _track
from tests.live_server import LiveServer
from tests.browser.quiet_action_support import action_evidence, assert_quiet_action

pytestmark = pytest.mark.browser


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_merge_app(monkeypatch, tmp_path, store):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    s = store
    iid = s.accounts.upsert_account("Main", "c1", None, True)
    a = s.playlists.upsert_playlist(iid, "PLA", "Mix A", 2, "h", 1.0)
    b = s.playlists.upsert_playlist(iid, "PLB", "Mix B", 1, "h", 1.0)
    t1 = s.tracks.upsert_track("v1", "Alpha Song", "X", None, None, 1)
    t2 = s.tracks.upsert_track("v2", "Beta Song", "X", None, None, 1)
    s.playlists.set_playlist_tracks(a, [t1, t2]); s.playlists.set_playlist_tracks(b, [t1])
    client = FakeClient(tracks={"PLA": [_track("v1", "Alpha Song", "X"), _track("v2", "Beta Song", "X")],
                                "PLB": [_track("v1", "Alpha Song", "X")]})
    app = create_app(s, lambda: {iid: client}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    with LiveServer(server):
        yield {"base": f"http://127.0.0.1:{port}", "ids": f"{a},{b}", "store": s, "client": client}


@pytest.mark.parametrize('failure', ['rejected', 'unexpected'])
def test_failed_delivery_keeps_editor_and_source(live_merge_app, page, monkeypatch, failure, action_evidence):
    client = live_merge_app["client"]
    client._tracks["PLA"] = [_track("v1", "Alpha Song", "X")]
    client._tracks["PLB"] = [_track("v1", "Alpha Song", "X"), _track("v2", "Beta Song", "X")]
    monkeypatch.setattr(client, "add_playlist_items", lambda *a: {"status": "STATUS_FAILED"})
    calls = []
    if failure == 'unexpected':
        from yt_playlist.library.ops import PlaylistOps
        def unavailable(*args, **kwargs):
            calls.append(1)
            raise TypeError('Private provider diagnostic')
        monkeypatch.setattr(PlaylistOps, 'apply_merge', unavailable)
    url = f"{live_merge_app['base']}/merge?ids={live_merge_app['ids']}"
    page.goto(url)
    if failure == 'unexpected':
        with page.expect_response('**/merge?*'):
            with page.expect_response('**/merge/apply?*') as response:
                page.get_by_role("button", name="Apply", exact=True).click()
        assert response.value.status == 503 and response.value.body() == b''
        expect(page.locator('#toasts [role="alert"], #connection-error')).to_have_count(0)
        expect(page.get_by_role('button', name='Apply', exact=True)).to_be_enabled()
        assert calls == [1]
    else:
        with page.expect_response('**/merge/apply?*'):
            page.get_by_role("button", name="Apply", exact=True).click()
        expect(page.locator('#merge-outcome')).to_contain_text('Refusing to delete')
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-merge-' + failure + '.png', full_page=True)
    expect(page).to_have_url(url)
    expect(page.locator("#merge-body")).to_be_visible()
    assert client.deleted == []
    assert live_merge_app["store"].actions.get_draft(live_merge_app["ids"]) is not None
    assert live_merge_app["store"].playlists.get_playlist(int(live_merge_app["ids"].split(",")[1])) is not None


def test_merge_returns_to_the_cleanup_list_before_and_after_applying(live_merge_app, page):
    base = live_merge_app["base"]
    page.goto(f"{base}/cleanup")
    page.get_by_role("link", name="Review", exact=True).first.click()
    back = page.get_by_role("link", name="Back to cleanup")
    expect(back).to_be_visible()
    expect(back).to_have_attribute("href", "/cleanup#overlaps")
    expect(page.get_by_text("2 / 2")).to_be_visible()
    page.locator('input[type=checkbox]').first.uncheck()
    expect(page.get_by_text("1 / 2")).to_be_visible()
    back.click()
    page.wait_for_url("**/cleanup#overlaps")
    page.get_by_role("link", name="Review", exact=True).first.click()
    page.get_by_role("button", name="Apply", exact=True).click()
    page.wait_for_url("**/cleanup?**#overlaps")
    expect(page.get_by_text("Merged into", exact=False)).to_be_visible()


def test_merge_from_playlists_returns_to_playlists(live_merge_app, page):
    base = live_merge_app["base"]
    page.goto(f"{base}/playlists")
    table = page.locator(".playlist-table-card")
    for name in ("Mix A", "Mix B"):
        table.get_by_role("row").filter(has_text=name).get_by_role("checkbox").check()
    page.get_by_role("button", name="Merge", exact=True).click()
    expect(page.get_by_role("link", name="Back to playlists")).to_have_attribute("href", "/playlists")
    page.get_by_role("button", name="Apply", exact=True).click()
    page.wait_for_url("**/playlists?**")
    expect(table.get_by_role("row").filter(has_text="Mix A")).to_be_visible()
    expect(table.get_by_role("row").filter(has_text="Mix B")).to_have_count(0)


@pytest.mark.parametrize('kind', ['json', 'value'])
def test_provider_read_failure_keeps_merge_draft_without_technical_toast(
        live_merge_app, page, monkeypatch, kind, action_evidence):
    import json
    from functools import partial
    from yt_playlist.library import executor
    from yt_playlist.util.retry import with_retry
    from tests.quiet_validation_support import DIAGNOSTIC

    case, store = live_merge_app, live_merge_app['store']
    url = f"{case['base']}/merge?ids={case['ids']}"
    page.goto(url)
    with page.expect_response('**/merge/update?*'):
        page.locator('input[type=checkbox]').first.uncheck()
    expect(page.get_by_text('1 / 2', exact=True)).to_be_visible()
    draft = store.actions.get_draft(case['ids'])
    playlists = store.playlists.get_playlists()
    tracks = {int(pid): store.charts.playlist_tracks_detail(int(pid))
              for pid in case['ids'].split(',')}
    reads, posts = [], []

    def fail_read(playlist_id, limit=100):
        reads.append(playlist_id)
        if kind == 'json':
            raise json.JSONDecodeError(DIAGNOSTIC, '<html>unavailable</html>', 0)
        raise ValueError(DIAGNOSTIC)

    monkeypatch.setattr(case['client'], 'get_playlist', fail_read)
    monkeypatch.setattr(executor, 'with_retry', partial(with_retry, sleep=lambda delay: None))
    page.on('request', lambda request: posts.append(request.url) if request.method == 'POST'
            and '/merge/' in request.url else None)
    # Register both listeners before the click, including the quiet GET recovery.
    with page.expect_response('**/merge?*'):
        with page.expect_response('**/merge/apply?*') as response:
            page.get_by_role('button', name='Apply', exact=True).click()
        assert response.value.status >= 500
        assert DIAGNOSTIC not in response.value.text()

    expect(page).to_have_url(url)
    expect(page.get_by_text('1 / 2', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Apply', exact=True)).to_be_enabled()
    expect(page.locator('#toasts .toast-err, #connection-error')).to_have_count(0)
    expect(page.get_by_text(DIAGNOSTIC, exact=False)).to_have_count(0)
    assert len(posts) == 1 and '/merge/apply?' in posts[0]
    assert reads == ['PLA'] * 3
    refreshed = store.actions.get_draft(case['ids'])
    assert refreshed.id == draft.id and refreshed.params_json == draft.params_json
    assert store.playlists.get_playlists() == playlists
    assert {pid: store.charts.playlist_tracks_detail(pid) for pid in tracks} == tracks
    client = case['client']
    assert not client.added and not client.deleted and not client.edited and not client.created
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-merge-provider-' + kind + '.png', full_page=True)


def test_lost_merge_deletion_receipt_keeps_draft_backup_and_single_attempt(
        live_merge_app, page, monkeypatch, action_evidence):
    import json
    from yt_playlist.core import fao

    case, client, store = live_merge_app, live_merge_app['client'], live_merge_app['store']
    page.set_viewport_size({'width': 1440, 'height': 1000})
    url = f"{case['base']}/merge?ids={case['ids']}"
    page.goto(url)
    draft = store.actions.get_draft(case['ids'])
    posts, attempts = [], []
    original = client.delete_playlist

    def lose_receipt(remote):
        attempts.append(remote)
        original(remote)
        raise TimeoutError('Private deletion receipt diagnostic')

    monkeypatch.setattr(client, 'delete_playlist', lose_receipt)
    page.on('request', lambda request: posts.append(request.url) if request.method == 'POST'
            and '/merge/' in request.url else None)
    with page.expect_response('**/merge?*'):
        with page.expect_response('**/merge/apply?*') as response:
            page.get_by_role('button', name='Apply', exact=True).click()
        assert response.value.status == 503 and response.value.body() == b''
    expect(page.get_by_role('button', name='Apply', exact=True)).to_be_enabled()
    expect(page.get_by_text("Apply isn't confirmed.", exact=False)).to_be_visible()
    assert attempts == client.deleted == ['PLB'] and len(posts) == 1
    retained = store.actions.get_draft(case['ids'])
    assert retained.id == draft.id and retained.params_json == draft.params_json
    action, = [item for item in store.actions.get_actions() if item.kind == 'apply_merge']
    assert action.status == 'delete_pending'
    deletion, = json.loads(action.undo_json)['deletions']
    assert deletion['phase'] == 'uncertain'
    backup = fao.read(deletion['backup']).bytes
    assert store.playlists.get_playlist(int(case['ids'].split(',')[1])) is not None
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-merge-uncertain-delete.png', full_page=True)
    page.reload()
    expect(page.locator('#merge-body')).to_be_visible()
    assert len(attempts) == len(posts) == 1
    assert store.actions.get_action(action.id).undo_json == action.undo_json
    assert fao.read(deletion['backup']).bytes == backup
