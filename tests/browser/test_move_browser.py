"""Move's compact table and selection ribbon, exercised against the live server."""
import socket
import threading

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
def live_move_app(store):
    s = store
    i1 = s.accounts.upsert_account("Main", "c1", None, True)
    i2 = s.accounts.upsert_account("Alt", "c2", None, False)
    tracks = {}
    for n, title in enumerate(("Evening", "Mix", "Weekend")):
        pid = s.playlists.upsert_playlist(i1, f"PL{n}", title, 1, "h", 1.0)
        t = s.tracks.upsert_track(f"v{n}", f"Song {n}", "Artist", None, None, 1)
        s.playlists.set_playlist_tracks(pid, [t])
        tracks[f"PL{n}"] = [_track(f"v{n}", f"Song {n}", "Artist")]
    s.playlists.upsert_playlist(i2, "PL_ALT", "Already here", 0, "h", 1.0)
    clients = {i1: FakeClient(tracks=tracks),
               i2: FakeClient(catalog={t[0]["videoId"]: t[0] for t in tracks.values()})}
    app = create_app(s, lambda: clients, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    with LiveServer(server):
        yield {"base": f"http://127.0.0.1:{port}", "i1": i1, "i2": i2, "clients": clients}


def _pick(page, i1, i2):
    page.get_by_label("From", exact=True).select_option(str(i1))
    page.get_by_label("To", exact=True).select_option(str(i2))


def test_move_copy_keeps_row_and_shows_message(live_move_app, page):
    page.goto(f"{live_move_app['base']}/move")
    _pick(page, live_move_app["i1"], live_move_app["i2"])
    link = page.get_by_role("link", name="Mix", exact=True)
    expect(link).to_be_visible()
    page.get_by_role("checkbox", name="Select Mix", exact=True).check()
    page.get_by_role("button", name="Copy", exact=True).click()
    expect(page.get_by_text("Copied 1 playlist.", exact=True)).to_be_visible()
    expect(link).to_be_visible()                         # copy keeps the row
    expect(page.get_by_role("region", name="Playlist actions")).to_be_hidden()
    # Refreshed panel includes the new destination immediately, without a page reload.
    page.get_by_label("From", exact=True).select_option(str(live_move_app["i2"]))
    expect(page.get_by_role("link", name="Mix", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Already here", exact=True)).to_be_visible()


def test_move_removes_row(live_move_app, page):
    page.goto(f"{live_move_app['base']}/move")
    _pick(page, live_move_app["i1"], live_move_app["i2"])
    page.get_by_role("checkbox", name="Select Mix", exact=True).check()
    page.get_by_role("button", name="Move", exact=True).click()
    page.get_by_role("link", name="Mix", exact=True).wait_for(state="hidden", timeout=3000)
    expect(page.get_by_text("Moved 1 playlist.", exact=True)).to_be_visible()


def test_move_disabled_for_same_identity(live_move_app, page):
    page.goto(f"{live_move_app['base']}/move")
    i1 = live_move_app["i1"]
    page.get_by_label("From", exact=True).select_option(str(i1))
    page.get_by_label("To", exact=True).select_option(str(i1))       # same as From
    page.get_by_role("checkbox", name="Select Mix", exact=True).check()
    expect(page.get_by_text("Pick two different identities")).to_be_visible()
    expect(page.get_by_role("button", name="Move")).to_be_disabled()
    expect(page.get_by_role("button", name="Copy", exact=True)).to_be_disabled()


def test_select_all_copies_only_source_playlists_and_resets_selection(live_move_app, page):
    page.goto(f"{live_move_app['base']}/move")
    page.get_by_role("checkbox", name="Select Mix", exact=True).check()
    all_check = page.get_by_role("checkbox", name="Select all playlists", exact=True)
    expect(all_check).to_have_js_property("indeterminate", True)
    all_check.check()
    expect(page.get_by_text("3 selected", exact=True)).to_be_visible()
    expect(all_check).to_have_js_property("indeterminate", False)
    page.get_by_role("button", name="Copy", exact=True).click()
    expect(page.get_by_text("Copied 3 playlists.", exact=True)).to_be_visible()
    assert [p[1] for p in live_move_app["clients"][live_move_app["i2"]].created] == ["Evening", "Mix", "Weekend"]
    expect(all_check).not_to_be_checked()
    # A second selection after the fragment swap has exactly one ribbon and works again.
    page.get_by_role("checkbox", name="Select Mix", exact=True).check()
    expect(page.get_by_role("region", name="Playlist actions")).to_have_count(1)
    page.get_by_role("button", name="Deselect all").click()
    expect(page.get_by_role("region", name="Playlist actions")).to_be_hidden()


def test_shift_range_and_changing_source_clears_hidden_selection(live_move_app, page):
    page.goto(f"{live_move_app['base']}/move")
    page.get_by_role("checkbox", name="Select Evening", exact=True).check()
    page.get_by_role("checkbox", name="Select Weekend", exact=True).click(modifiers=["Shift"])
    expect(page.get_by_text("3 selected", exact=True)).to_be_visible()
    expect(page.get_by_role("checkbox", name="Select Mix", exact=True)).to_be_checked()
    page.get_by_label("From", exact=True).select_option(str(live_move_app["i2"]))
    expect(page.get_by_role("region", name="Playlist actions")).to_be_hidden()
    page.get_by_label("To", exact=True).select_option(str(live_move_app["i1"]))
    page.get_by_role("checkbox", name="Select all playlists", exact=True).check()
    expect(page.get_by_text("1 selected", exact=True)).to_be_visible()
    page.get_by_role("button", name="Move", exact=True).click()
    expect(page.get_by_text("Moved 1 playlist.", exact=True)).to_be_visible()
    expect(page.get_by_text("No playlists in this identity.", exact=True)).to_be_visible()
    assert not live_move_app["clients"][live_move_app["i1"]].deleted


def test_bulk_move_shows_empty_source(live_move_app, page):
    page.goto(f"{live_move_app['base']}/move")
    page.get_by_role("checkbox", name="Select all playlists", exact=True).check()
    page.get_by_role("button", name="Move", exact=True).click()
    expect(page.get_by_text("Moved 3 playlists.", exact=True)).to_be_visible()
    expect(page.get_by_text("No playlists in this identity.", exact=True)).to_be_visible()
    expect(page.get_by_role("table", name="Playlists to copy or move")).to_be_hidden()


def test_bulk_failure_shows_details_and_preserves_failed_source(live_move_app, page, monkeypatch):
    client = live_move_app["clients"][live_move_app["i2"]]
    create = client.create_playlist

    def fail_mix(title, description):
        if title == "Mix":
            raise ValueError("This playlist could not be created.")
        return create(title, description)

    monkeypatch.setattr(client, "create_playlist", fail_mix)
    page.goto(f"{live_move_app['base']}/move")
    page.get_by_role("checkbox", name="Select all playlists", exact=True).check()
    page.get_by_role("button", name="Move", exact=True).click()
    expect(page.get_by_text("Moved 2 playlists. 1 failed.", exact=True)).to_be_visible()
    expect(page.get_by_text("Transfer not confirmed. Review Actions before trying again.", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Mix", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="Evening", exact=True)).to_be_hidden()
    expect(page.get_by_role("region", name="Playlist actions")).to_be_hidden()


def test_pending_transfer_disables_repeat_actions_and_identity_changes(live_move_app, page, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    client = live_move_app["clients"][live_move_app["i2"]]
    create = client.create_playlist

    def slow_create(title, description):
        entered.set()
        release.wait(timeout=10)
        return create(title, description)

    monkeypatch.setattr(client, "create_playlist", slow_create)
    page.goto(f"{live_move_app['base']}/move")
    page.get_by_role("checkbox", name="Select all playlists", exact=True).check()
    page.get_by_role("button", name="Copy", exact=True).click()
    try:
        assert entered.wait(timeout=3)
        expect(page.get_by_text("Copying 3 playlists…", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Copy", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="Move", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="Deselect all")).to_be_disabled()
        expect(page.get_by_label("From", exact=True)).to_be_disabled()
        expect(page.get_by_label("To", exact=True)).to_be_disabled()
        expect(page.get_by_role("checkbox", name="Select Mix", exact=True)).to_be_disabled()
    finally:
        release.set()
    expect(page.get_by_text("Copied 3 playlists.", exact=True)).to_be_visible()
    assert len(client.created) == 3


@pytest.mark.parametrize('remote_deleted', [False, True])
def test_uncertain_move_keeps_copy_and_rechecks_without_repeating_deletion(
        live_move_app, store, page, monkeypatch, remote_deleted, action_evidence):
    import json
    from pathlib import Path

    app = live_move_app
    source, target = app['clients'][app['i1']], app['clients'][app['i2']]
    original = next(p.id for p in store.playlists.get_playlists()
                    if p.account_id == app['i1'] and p.provider_playlist_id == 'PL1')
    delete = source.delete_playlist
    attempts = []
    diagnostic = 'sqlite3.OperationalError: internal-diagnostic-879-move'

    def uncertain_delete(playlist):
        attempts.append(playlist)
        if remote_deleted:
            delete(playlist)
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
    expect(row.locator('td').nth(3)).to_have_text('executed' if remote_deleted else 'deletion failed')
    final = store.actions.get_action(action.id)
    assert final.status == ('executed' if remote_deleted else 'delete_failed')
    assert (store.playlists.get_playlist(original) is None) == remote_deleted
    assert len(target.created) == len(target.added) == 1 and attempts == ['PL1']
    assert Path(entry['backup']).read_bytes() == backup
    expect(page.locator('body')).not_to_contain_text(diagnostic)
    expect(page.locator('#navigation-error, .toast-err, .notice.err.banner:visible')).to_have_count(0)
    expect(page.locator('.notice.ok.banner')).to_contain_text(
        'Deletion confirmed; the backup is ready for Undo.' if remote_deleted else
        'The playlist still exists. No deletion was retried; the backup is saved.')
    page.screenshot(path=action_evidence + '-move-reconciled.png')


@pytest.mark.parametrize("width", [1280])
def test_compact_layout_and_ribbon_fit_viewport(live_move_app, page, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{live_move_app['base']}/move")
    expect(page.locator(".page-head p")).to_have_text("Copy or move playlists between your identities.")
    row = page.get_by_role("row").filter(has_text="Mix")
    expect(row.get_by_role("button")).to_have_count(0)
    expect(row.locator(".ui-arrow, .pid")).to_have_count(0)
    expect(row.get_by_role("link", name="Mix", exact=True)).to_have_attribute("title", "YouTube playlist ID: PL1")
    assert row.bounding_box()["height"] < 60
    page.get_by_role("checkbox", name="Select all playlists", exact=True).check()
    bar = page.get_by_role("region", name="Playlist actions")
    expect(bar).to_be_visible()
    box = bar.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=f"/tmp/tuneconsole-move-{width}.png", full_page=True)
