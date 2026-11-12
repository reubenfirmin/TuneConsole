"""The same song menu works across views; playlist writes and modal focus stay predictable."""
import socket
from tests.enrichment_support import seed_enrichment
from urllib.parse import parse_qs, urlparse

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.test_song_actions import SONG, seed_song_actions
from tests.live_server import LiveServer
from tests.browser.quiet_action_support import action_evidence, assert_quiet_action


@pytest.fixture
def song_app(store):
    data = seed_song_actions(store)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(data["app"], host="127.0.0.1", port=port, log_level="warning"))
    with LiveServer(server):
        yield {**data, "base": f"http://127.0.0.1:{port}"}












@pytest.mark.parametrize("control,width", [("menu", 1040), ("cover", 1440)])
def test_playlist_play_starts_selected_track_in_playlist(song_app, page, control, width, tmp_path):
    store, source = song_app["store"], song_app["source"]
    title = "Revenge Of The Wankers (Kora Remix) (feat. Scarlett De La Torre)"
    track = store.tracks.upsert_track("second", title, "Nirvana", "A long album name with useful identity", 270,
                                     thumbnail="/static/road-trip-hero.png")
    store.tracks.set_track_genre(track, "Turkish")
    store.playlists.set_playlist_tracks(source, store.playlists.get_playlist_track_ids(source) + [track])
    page.set_viewport_size({"width": width, "height": 1000})
    page.route("**/play", lambda route: route.fulfill(json={"ok": True}))
    page.goto(f"{song_app['base']}/playlist/{source}")
    row = page.locator('tr.srow[data-vid="second"]')
    assert row.locator('td').nth(4).bounding_box()['width'] >= 80
    assert row.bounding_box()['height'] < 250
    expect(row.get_by_role('link', name='Nirvana', exact=True)).to_be_visible()
    page.get_by_role('button', name='Artist', exact=True).click()
    expect(row).to_be_visible()

    def play():
        with page.expect_request(lambda request: request.url.endswith("/play") and request.method == "POST") as request:
            if control == "cover":
                row.hover()
                row.locator(".pl-play").click()
            else:
                row.get_by_role("button", name=f"More actions for {title}").click()
                page.get_by_role("menuitem", name="Play on YouTube Music").click()
        url = urlparse(request.value.post_data_json["url"])
        assert url.netloc == "music.youtube.com" and url.path == "/watch"
        assert parse_qs(url.query) == {"v": ["second"], "list": ["SOURCE"]}

    play()
    page.screenshot(path=tmp_path / f"playlist-top-aligned-{control}.png", animations="disabled")
    # Both manual updates and enrichment replace a complete row with this fragment.
    response = page.request.post(f"{song_app['base']}/playlist/{source}/track-year",
                                 form={"video_id": "second", "year": "2019"})
    assert response.ok
    page.evaluate("html => enrichPanel(1).applyRow({video_id: 'second', row_html: html})",
                  response.text())
    expect(row.get_by_text("2019", exact=True)).to_be_visible()
    play()




















@pytest.mark.parametrize('surface', ['playlist'])
@pytest.mark.parametrize('width', [1280])
def test_metadata_dialog_edits_all_fields_and_restores_originals(song_app, page, surface, width, tmp_path):
    store = song_app['store']
    tid = store.tracks.upsert_track('owned-song', 'Owned Song', 'Artist & Friends', 'Album', 180,
                                   album_browse_id='MPRE_owned')
    store.collection.add_saved_album({'browse': 'MPRE_owned', 'title': 'Album', 'artist': 'Artist & Friends',
                                      'year': '2000', 'thumbnail': ''})
    seed_enrichment(store, tid, 'Electronic', '1995')
    store.enrichment_state.log_enrichment(tid, 'run', 'discogs', 'genre', 'Art Pop', now=1)
    path = f"/playlist/{song_app['source']}" if surface == 'playlist' else '/album?browse=MPRE_owned'
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 850})
    page.goto(song_app['base'] + path)
    row = page.locator('tr.srow[data-vid="owned-song"]')
    expect(row.locator('input[type="text"], .editpen, .resetpen, .genre-info')).to_have_count(0)
    trigger = row.get_by_role('button', name='More actions for Owned Song')
    trigger.click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    expect(dialog.get_by_label('Title', exact=True)).to_be_focused()
    expect(dialog.get_by_label('Title', exact=True)).to_have_value('Owned Song')
    expect(dialog.get_by_label('Artist', exact=True)).to_have_value('Artist & Friends')
    expect(dialog.get_by_label('Genre', exact=True)).to_have_value('Electronic')
    expect(dialog.get_by_label('Year', exact=True)).to_have_value('1995')
    dialog.get_by_label('Title', exact=True).fill('Unsaved title')
    page.keyboard.press('Escape')
    expect(trigger).to_be_focused()
    assert store.tracks.metadata_for_video('owned-song')['title'] == 'Owned Song'
    trigger.click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    expect(dialog.get_by_label('Title', exact=True)).to_have_value('Owned Song')
    dialog.get_by_label('Title', exact=True).fill('Fixed title')
    dialog.get_by_label('Artist', exact=True).fill('Fixed artist')
    dialog.get_by_role('button', name='Art Pop discogs').click()
    dialog.get_by_label('Year', exact=True).fill('1998')
    assert dialog.evaluate('el => el.scrollWidth <= el.clientWidth')
    for _ in range(12):
        page.keyboard.press('Tab')
        assert dialog.evaluate('el => el.contains(document.activeElement)')
    page.screenshot(path=tmp_path / f'metadata-{surface}-{width}.png', animations='disabled')
    dialog.get_by_role('button', name='Save changes').click()
    expect(row.locator('.ptitle')).to_have_text('Fixed title')
    expect(row.locator('.alink').first).to_have_text('Fixed artist')
    expect(row.locator('.gtag')).to_have_text('Art Pop')
    expect(row.locator('.ydisplay')).to_have_text('1998')
    expect(row).to_have_attribute('data-genre', 'art pop')
    expect(row).to_have_attribute('data-year', '1998')
    row.get_by_role('button', name='More actions for Fixed title').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog.get_by_role('button', name='Restore original title').click()
    dialog.get_by_role('button', name='Restore original artist').click()
    dialog.get_by_label('Genre', exact=True).fill('')
    dialog.get_by_label('Year', exact=True).fill('')
    dialog.get_by_role('button', name='Save changes').click()
    expect(row.locator('.ptitle')).to_have_text('Owned Song', use_inner_text=True)
    expect(row.locator('.gtag')).to_have_count(0)
    expect(row.locator('.ydisplay')).to_have_text('—')
    assert not errors




def test_metadata_save_failure_preserves_edits_for_retry(song_app, page):
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    page.get_by_role('button', name='More actions for Owned Song').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    title = dialog.get_by_label('Title', exact=True)
    expect(title).to_have_value('Owned Song')
    title.fill('Retry this title')
    page.route('**/songs/metadata', lambda route: route.fulfill(status=503, json={'detail': 'Try again later.'}))
    dialog.get_by_role('button', name='Save changes').click()
    expect(dialog).not_to_be_visible()
    expect(page.locator('.song-edit-status').first).to_have_text('Edit not confirmed')
    expect(page.locator('#toasts [role="alert"]')).to_have_count(0)
    page.get_by_role('button', name='More actions for Owned Song').click()
    expect(page.get_by_role('menuitem', name='Edit Metadata')).to_contain_text('Edit not confirmed')
    page.screenshot(path='/tmp/tuneconsole-879-metadata-unconfirmed.png')
    page.get_by_role('menuitem', name='Edit Metadata').click()
    expect(dialog.get_by_role('status')).to_have_text(
        'This edit was not confirmed. Review your changes before saving again.')
    expect(title).to_have_value('Retry this title')
    assert song_app['store'].tracks.metadata_for_video('owned-song')['title'] == 'Owned Song'
    page.unroute('**/songs/metadata')
    dialog.get_by_role('button', name='Save changes').click()
    expect(page.locator('tr.srow .ptitle')).to_have_text('Retry this title')
    expect(page.locator('.song-edit-status')).to_have_count(0)
