"""Live-behavior tests for the Cleanup page, section by section. Own fixture.
Characterization first: lock current Alpine behavior, keep passing after htmx conversion."""
import socket

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient
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
def live_cleanup_app(store):
    s = store
    iid = s.accounts.upsert_account("Main", "c1", None, True)
    s.playlists.upsert_playlist(iid, "PLE", "Empty One", 0, "h", 1.0)   # no tracks -> an empty playlist
    # two identical playlists -> an exact-duplicate group
    dup_tracks = [s.tracks.upsert_track(f"d{i}", f"D{i}", "X", None, None, 1) for i in range(2)]
    for ytm, title in (("PLD1", "Dup A"), ("PLD2", "Dup B")):
        pid = s.playlists.upsert_playlist(iid, ytm, title, 2, "h", 1.0)
        s.playlists.set_playlist_tracks(pid, dup_tracks)
    # two playlists that share tracks but aren't identical -> an overlap pair
    ov = [s.tracks.upsert_track(f"o{i}", f"O{i}", "X", None, None, 1) for i in range(4)]
    oa = s.playlists.upsert_playlist(iid, "POVA", "Ov A", 3, "h", 1.0); s.playlists.set_playlist_tracks(oa, ov[0:3])
    ob = s.playlists.upsert_playlist(iid, "POVB", "Ov B", 3, "h", 1.0); s.playlists.set_playlist_tracks(ob, ov[1:4])
    # Three near-duplicates share seven tracks and each have a different eighth track.
    near = [s.tracks.upsert_track(f"n{i}", f"N{i}", "X", None, None, 1) for i in range(10)]
    for n, ytm in enumerate(("PNEARA", "PNEARB", "PNEARC")):
        pid = s.playlists.upsert_playlist(iid, ytm, f"Near {chr(65 + n)}", 8, "h", 1.0)
        s.playlists.set_playlist_tracks(pid, near[:7] + [near[7 + n]])
    app = create_app(s, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    with LiveServer(server):
        yield f"http://127.0.0.1:{port}"


def test_empty_playlist_delete_removes_row(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    link = page.get_by_role("link", name="Empty One", exact=True)
    expect(link).to_be_visible()
    page.get_by_role("row").filter(has_text="Empty One").get_by_role("button", name="Delete").click()
    link.wait_for(state="hidden", timeout=3000)


def test_empty_playlist_failed_read_keeps_row_and_allows_retry(live_cleanup_app, page, monkeypatch, action_evidence):
    from yt_playlist.library import executor
    monkeypatch.setattr(executor, "with_retry", lambda fn: fn())
    get_playlist = FakeClient.get_playlist

    def unreadable(self, playlistId, limit=None):
        if playlistId == "PLE":
            raise TimeoutError("YouTube read timed out")
        return get_playlist(self, playlistId, limit)

    monkeypatch.setattr(FakeClient, "get_playlist", unreadable)
    page.goto(f"{live_cleanup_app}/cleanup")
    row = page.get_by_role("row").filter(has_text="Empty One")
    with page.expect_response('**/cleanup'):
        with page.expect_response('**/playlist/delete-empty') as response:
            row.get_by_role("button", name="Delete", exact=True).click()
    assert response.value.status == 503
    assert_quiet_action(page)
    expect(row.get_by_text("This change isn't confirmed.", exact=True)).to_be_visible()
    expect(row.get_by_role('button', name='Remove from TuneConsole', exact=True)).to_be_visible()
    page.screenshot(path=action_evidence + '-empty-read-failure.png', full_page=True)
    expect(row).to_be_visible()
    expect(row.get_by_role("button", name="Delete", exact=True)).to_be_enabled()

    monkeypatch.setattr(FakeClient, "get_playlist", get_playlist)
    row.get_by_role("button", name="Delete", exact=True).click()
    expect(row).to_have_count(0)


def test_failed_remote_read_offers_local_removal(live_cleanup_app, page, monkeypatch):
    from yt_playlist.library import executor
    monkeypatch.setattr(executor, 'with_retry', lambda fn: fn())

    def unreadable(*args, **kwargs):
        raise KeyError('musicPlaylistShelfRenderer')

    def never_delete(*args, **kwargs):
        raise AssertionError('Local removal must not call YouTube delete')

    monkeypatch.setattr(FakeClient, 'get_playlist', unreadable)
    monkeypatch.setattr(FakeClient, 'delete_playlist', never_delete)
    page.goto(f'{live_cleanup_app}/cleanup')
    row = page.get_by_role('row').filter(has_text='Empty One')
    removal = row.get_by_role('button', name='Remove from TuneConsole', exact=True)
    expect(removal).to_be_hidden()
    with page.expect_response('**/cleanup'):
        row.get_by_role('button', name='Delete', exact=True).click()
    expect(removal).to_be_visible()
    removal.click()
    expect(page.get_by_text('YouTube will stay unchanged.', exact=False)).to_be_visible()
    page.get_by_role('button', name='Confirm', exact=True).click()
    expect(row).to_have_count(0)
    page.goto(f'{live_cleanup_app}/actions')
    expect(page.get_by_text('YouTube unchanged', exact=False)).to_be_visible()


def test_keep_one_resolves_duplicate_group(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    expect(page.get_by_text("identical copies")).to_be_visible()
    page.get_by_role("button", name="Keep this one").first.click()
    # keeping one deletes the other copy and the group resolves (page recomputes)
    page.get_by_text("identical copies").wait_for(state="hidden", timeout=4000)


def test_keep_one_resolves_all_saved_memberships_after_owner_deletes_copy(
        store, page):
    from tests.conftest import _track
    from ytmusicapi.exceptions import YTMusicServerError

    accounts = [store.accounts.upsert_account(label, 'bridge', None, index == 0)
                for index, label in enumerate(('Keeper owner', 'Saved memberships', 'Copy owner'))]
    track = store.tracks.upsert_track('one', 'One', 'Artist', '', 200)
    memberships = []
    for account, remote in ((accounts[0], 'KEEP'), (accounts[1], 'KEEP'),
                            (accounts[1], 'COPY'), (accounts[2], 'COPY')):
        pid = store.playlists.upsert_playlist(account, remote, remote, 1, '', 1)
        store.playlists.set_playlist_tracks(pid, [track])
        memberships.append(pid)
    absent, attempts = set(), []

    class SharedClient(FakeClient):
        def __init__(self, account):
            super().__init__(tracks={remote: [_track('one', 'One', 'Artist')]
                                    for remote in ('KEEP', 'COPY')})
            self.account = account

        def get_playlist(self, playlistId, limit=100):
            if playlistId in absent:
                raise YTMusicServerError('Server returned HTTP 404: Not Found.')
            return super().get_playlist(playlistId, limit)

        def delete_playlist(self, playlistId):
            attempts.append((self.account, playlistId))
            owner = accounts[0] if playlistId == 'KEEP' else accounts[2]
            if self.account != owner:
                raise YTMusicServerError('Server returned HTTP 403: Forbidden.')
            absent.add(playlistId)
            self.deleted.append(playlistId)

    clients = {account: SharedClient(account) for account in accounts}
    app = create_app(store, lambda: clients, now_fn=lambda: 2, background_work=False)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    with LiveServer(server):
        page.set_viewport_size({'width': 1440, 'height': 1100})
        page.goto(f'http://127.0.0.1:{port}/cleanup')
        group = page.locator('.workflow-case').filter(has_text='identical copies')
        expect(group.get_by_role('button', name='Keep this one')).to_have_count(2)
        with page.expect_navigation(wait_until='domcontentloaded'):
            group.get_by_role('button', name='Keep this one').first.click()
        expect(group).to_have_count(0)
        expect(page.get_by_text('No exact duplicates to review.', exact=False)).to_be_visible()
        assert attempts == [(accounts[1], 'COPY'), (accounts[2], 'COPY')]
        assert absent == {'COPY'} and clients[accounts[2]].deleted == ['COPY']
        assert {playlist.id for playlist in store.playlists.get_playlists()} == set(memberships[:2])
        assert not errors
        page.locator('main').evaluate('node => Promise.all(node.getAnimations({subtree: true}).map(animation => animation.finished))')
        page.screenshot(path='/tmp/tuneconsole-872-resolved.png', full_page=True)


def test_overlap_hide_pair_via_pie_menu(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    row = page.locator('[data-section="overlaps"] tbody.ov-row')
    expect(row).to_have_count(1)
    row.locator(".kebab").click()                                   # open the radial pie menu
    row.locator(".wedge.w-hide").dispatch_event("click")           # "hide this pair" wedge
    # htmx.ajax -> HX-Refresh -> reload; the pair is now suppressed and appears under "Hidden"
    expect(page.get_by_role("button", name="Hidden overlaps 1")).to_be_visible()
    expect(page.locator('[data-section="overlaps"] tbody.ov-row')).to_have_count(0)
    expect(page.locator('[data-section="near"] tbody.ov-row')).to_have_count(3)


def test_near_duplicate_hide_and_restore_via_same_pie_menu(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    rows = page.locator('[data-section="near"] tbody.ov-row')
    expect(rows).to_have_count(3)
    row = rows.first
    row.get_by_role("button", name="More actions").click()
    expect(row.get_by_role("menu", name="Overlap actions")).to_be_visible()
    row.locator(".wedge.w-hide").click()
    expect(rows).to_have_count(2)
    expect(page.locator('[data-section="overlaps"] tbody.ov-row')).to_have_count(1)
    page.get_by_role("button", name="Hidden overlaps 1").click()
    page.get_by_role("button", name="Unhide", exact=True).click()
    expect(rows).to_have_count(3)


def test_near_duplicate_bulk_hide_only_affects_its_table(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    rows = page.locator('[data-section="near"] tbody.ov-row')
    rows.first.get_by_role("button", name="More actions").click()
    rows.first.locator(".wedge.w-below").click()
    expect(page.get_by_role("heading", name="Hide these pairs?")).to_be_visible()
    page.get_by_role("button", name="Yes, hide them").click()
    expect(rows).to_have_count(0)
    expect(page.locator('[data-section="overlaps"] tbody.ov-row')).to_have_count(1)


def test_near_duplicate_review_returns_to_near_section(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    page.locator('[data-section="near"]').get_by_role("link", name="Review", exact=True).first.click()
    back = page.get_by_role("link", name="Back to cleanup")
    expect(back).to_have_attribute("href", "/cleanup#near")
    back.click()
    expect(page).to_have_url(f"{live_cleanup_app}/cleanup#near")
