"""The same song menu works across views; playlist writes and modal focus stay predictable."""
import socket
import threading
from tests.enrichment_support import seed_enrichment
from urllib.parse import parse_qs, urlparse

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.conftest import _track
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
    artist_id = store.artists.track_credits(tid)[0]['artist_id']
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
    expect(row.locator('[data-artist-credit] a')).to_have_attribute('href', f'/artist?artist_id={artist_id}')
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




def test_metadata_save_failure_preserves_edits_for_retry(song_app, page, action_evidence):
    posts = []
    page.on('request', lambda request: posts.append(request.url)
            if request.method == 'POST' and request.url.endswith('/songs/metadata') else None)
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    assert page.evaluate('!!(window.originalPlayer = document.querySelector(".global-now-playing"))')
    page.get_by_role('button', name='More actions for Owned Song').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    title = dialog.get_by_label('Title', exact=True)
    expect(title).to_have_value('Owned Song')
    title.fill('Retry this title')
    page.route('**/songs/metadata', lambda route: route.fulfill(
        status=503, json={'detail': 'internal-diagnostic-879: write rejected'}))
    dialog.get_by_role('button', name='Save changes').click()
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role('status')).to_have_text(
        'This edit was not confirmed. Review your changes before saving again. Currently saved title “Owned Song”.')
    expect(title).to_have_value('Retry this title')
    expect(dialog.get_by_role('button', name='Save changes')).to_be_enabled()
    assert len(posts) == 1
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-metadata-unconfirmed.png')
    dialog.get_by_role('button', name='Cancel', exact=True).click()
    expect(page.locator('.song-edit-status').first).to_have_text('Edit not confirmed')
    expect(page.locator('#toasts [role="alert"]')).to_have_count(0)
    page.get_by_role('button', name='More actions for Owned Song').click()
    expect(page.get_by_role('menuitem', name='Edit Metadata')).to_contain_text('Edit not confirmed')
    page.get_by_role('menuitem', name='Edit Metadata').click()
    expect(dialog.get_by_role('status')).to_have_text(
        'This edit was not confirmed. Review your changes before saving again. Currently saved title “Owned Song”.')
    expect(title).to_have_value('Retry this title')
    assert song_app['store'].tracks.metadata_for_video('owned-song')['title'] == 'Owned Song'
    assert len(posts) == 1
    page.unroute('**/songs/metadata')
    dialog.get_by_role('button', name='Save changes').click()
    expect(page.locator('tr.srow .ptitle')).to_have_text('Retry this title')
    expect(dialog).not_to_be_visible()
    expect(page.locator('.song-edit-status')).to_have_count(0)
    assert len(posts) == 2
    assert page.evaluate('window.originalPlayer === document.querySelector(".global-now-playing")')


@pytest.mark.parametrize('recovery', ['immediate', 'reopen', 'check-button'])
def test_metadata_lost_receipt_reads_back_without_another_write(song_app, page, recovery):
    requests = []
    reads_available = True

    def intercept(route):
        requests.append(route.request.method)
        if route.request.method == 'POST':
            assert route.fetch().ok
            route.abort()
        elif reads_available:
            route.continue_()
        else:
            route.abort()

    page.route('**/songs/metadata*', intercept)
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    assert page.evaluate('!!(window.originalPlayer = document.querySelector(".global-now-playing"))')
    row = page.locator('tr.srow[data-vid="owned-song"]')
    row.get_by_role('button', name='More actions for Owned Song').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    title = dialog.get_by_label('Title', exact=True)
    expect(title).to_have_value('Owned Song')
    title.fill('  Recovered title  ')
    reads_available = recovery == 'immediate'
    dialog.get_by_role('button', name='Save changes').click()
    expected_requests = ['GET', 'POST', 'GET']
    if recovery != 'immediate':
        check = dialog.get_by_role('button', name='Check saved changes')
        expect(check).to_be_enabled()
        expect(title).to_have_value('  Recovered title  ')
        expect(row.locator('.ptitle')).to_have_text('Owned Song')
        assert requests == expected_requests
        reads_available = True
        expected_requests.append('GET')
        if recovery == 'reopen':
            dialog.get_by_role('button', name='Cancel').click()
            row.get_by_role('button', name='More actions for Owned Song').click()
            page.get_by_role('menuitem', name='Edit Metadata').click()
            expect(title).to_have_value('Recovered title')
            expect(dialog.get_by_role('button', name='Save changes')).to_be_enabled()
        else:
            check.click()
    expect(row.locator('.ptitle')).to_have_text('Recovered title')
    expect(row.locator('.song-edit-status')).to_have_count(0)
    if recovery != 'reopen':
        expect(dialog).not_to_be_visible()
    assert requests == expected_requests
    assert song_app['store'].tracks.metadata_for_video('owned-song')['title'] == 'Recovered title'
    assert page.evaluate('window.originalPlayer === document.querySelector(".global-now-playing")')
    assert_quiet_action(page)


@pytest.mark.parametrize('accepted', [True, False])
def test_metadata_pending_save_cannot_retire_a_replacement_dialog(
        song_app, page, monkeypatch, action_evidence, accepted):
    store = song_app['store']
    tid = store.tracks.upsert_track('other-song', 'Other Song', 'Another Artist', None, 180)
    store.playlists.set_playlist_tracks(song_app['source'],
        store.playlists.get_playlist_track_ids(song_app['source']) + [tid])
    entered, release = threading.Event(), threading.Event()
    update = store.tracks.update_metadata
    writes = []

    def pending(video, values, **kwargs):
        writes.append(video)
        entered.set()
        assert release.wait(10)
        if not accepted:
            raise RuntimeError('internal-diagnostic-879: delayed write rejected')
        return update(video, values, **kwargs)

    monkeypatch.setattr(store.tracks, 'update_metadata', pending)
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    row = page.locator('tr.srow[data-vid="owned-song"]')
    row.get_by_role('button', name='More actions for Owned Song').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    expect(dialog.get_by_label('Title', exact=True)).to_have_value('Owned Song')
    dialog.get_by_label('Genre', exact=True).fill('Dream Metal')
    try:
        dialog.get_by_role('button', name='Save changes').click(click_count=2)
        assert entered.wait(5)
        expect(dialog).to_be_visible()
        expect(dialog.get_by_role('button', name='Saving…')).to_be_disabled()
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        row.get_by_role('button', name='More actions for Owned Song').click()
        page.get_by_role('menuitem', name='Edit Metadata').click()
        expect(dialog.get_by_text('Loading metadata…')).to_be_visible()
        expect(dialog.get_by_role('button', name='Save changes')).to_be_disabled()
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        page.get_by_role('button', name='More actions for Other Song').click()
        page.get_by_role('menuitem', name='Edit Metadata').click()
        expect(dialog.get_by_label('Title', exact=True)).to_have_value('Other Song')
        dialog.get_by_label('Genre', exact=True).fill('Other draft')
        release.set()
        if accepted:
            expect(row.locator('.gtag')).to_have_text('Dream Metal')
        else:
            expect(row.locator('.song-edit-status')).to_have_text('Edit not confirmed')
        expect(dialog).to_be_visible()
        expect(dialog.get_by_label('Title', exact=True)).to_have_value('Other Song')
        expect(dialog.get_by_label('Genre', exact=True)).to_have_value('Other draft')
        expect(dialog.get_by_role('button', name='Save changes')).to_be_enabled()
        assert writes == ['owned-song']
        assert_quiet_action(page)
        page.screenshot(path=action_evidence + '-metadata-replacement.png')
    finally:
        release.set()


@pytest.mark.parametrize('action,outcome,replacement_pending', [
    ('create', 'confirmed', False), ('add', 'confirmed', True),
    ('create', 'rejected', True), ('add', 'partial', False),
])
def test_playlist_reply_stays_with_its_submission_after_back(
        song_app, page, action, outcome, replacement_pending):
    store = song_app['store']
    catalog = {video: _track(video, title, artist, 180, set_video_id=video + '-set')
               for video, title, artist in [('owned-song', 'Owned Song', 'Artist & Friends'),
                                            ('other-song', 'Other Song', 'Other Artist')]}
    song_app['main']._catalog.update(catalog)
    song_app['other']._catalog.update(catalog)
    song_app['other']._tracks['DEST'] = [catalog['other-song']]
    other = store.tracks.upsert_track('other-song', 'Other Song', 'Other Artist', '', 180)
    store.playlists.set_playlist_tracks(song_app['destination'], [other])
    source = f"{song_app['base']}/playlist/{song_app['source']}"
    destination = f"{song_app['base']}/playlist/{song_app['destination']}"
    held = []
    def hold(route):
        held.append(route)

    page.set_viewport_size({'width': 1440, 'height': 960})
    page.route('**/songs/create-playlist', hold)
    page.route('**/songs/add-to-playlist', hold)
    page.goto(destination)
    page.evaluate('''() => {
        window.originalSongDialog = document.querySelector('.song-playlist-dialog');
        window.songUpdates = [];
        window.playlistReplies = 0;
        window.addEventListener('song-playlist-updated', event => songUpdates.push(event.detail.video_id));
        const fetch = window.fetch.bind(window);
        window.fetch = async (url, options) => {
            const response = await fetch(url, options);
            if (url !== '/songs/create-playlist' && url !== '/songs/add-to-playlist') return response;
            const parse = response.json.bind(response);
            response.json = async () => {
                const data = await parse();
                requestAnimationFrame(() => window.playlistReplies++);
                return data;
            };
            return response;
        };
    }''')
    page.evaluate('url => navigation.navigate(url).finished', source)
    dialog = page.locator('.song-playlist-dialog')

    def open_action(song, menu_item):
        page.get_by_role('button', name=f'More actions for {song}').click()
        page.get_by_role('menuitem', name=menu_item, exact=True).click()

    def submit(button, video):
        with page.expect_request(lambda request: request.method == 'POST' and '/songs/' in request.url):
            dialog.get_by_role('button', name=button, exact=True).click()
        expect(dialog.get_by_role('button', name='Saving…')).to_be_disabled()
        assert len(held) == 1
        route = held.pop()
        assert route.request.post_data_json['song']['video_id'] == video
        return route

    if action == 'create':
        open_action('Owned Song', 'Create playlist from song')
        dialog.get_by_label('Playlist name').fill('First action mix')
        first = submit('Create playlist', 'owned-song')
        assert first.request.post_data_json['name'] == 'First action mix'
    else:
        open_action('Owned Song', 'Add song to playlist')
        dialog.get_by_role('radio', name='Evening mix', exact=False).check()
        first = submit('Add song', 'owned-song')
        assert first.request.post_data_json['playlist_id'] == song_app['destination']
    if outcome == 'confirmed':
        receipt = first.fetch()
        assert receipt.ok, receipt.text()
        playlist = int(receipt.json()['url'].rsplit('/', 1)[-1])
        videos = [row['video_id'] for row in store.charts.playlist_tracks_detail(playlist)]
        assert videos == (['owned-song'] if action == 'create' else ['other-song', 'owned-song'])

    page.evaluate('() => navigation.back().finished')
    assert dialog.evaluate('element => element === window.originalSongDialog')
    open_action('Other Song', 'Create playlist from song')
    name = dialog.get_by_label('Playlist name')
    expect(name).to_have_value('Other Song')
    name.fill('Second action draft')
    second = submit('Create playlist', 'other-song') if replacement_pending else None

    if outcome == 'confirmed':
        first.fulfill(response=receipt)
    elif outcome == 'rejected':
        first.fulfill(status=422, json={'code': 'main_account_required'})
    else:
        first.fulfill(json={'status': 'partial', 'added': False, 'title': 'Evening mix',
                            'message': 'YouTube has not confirmed this song’s addition.', 'url': destination})
    page.wait_for_function('window.playlistReplies === 1')
    expect(name).to_be_visible()
    expect(name).to_have_value('Second action draft')
    expect(dialog.locator('.song-action-error')).to_be_hidden()
    updates = ['owned-song'] if outcome == 'confirmed' else []
    assert page.evaluate('window.songUpdates') == updates

    if replacement_pending:
        expect(dialog.get_by_role('button', name='Saving…')).to_be_disabled()
    else:
        second = submit('Create playlist', 'other-song')
    assert second.request.post_data_json['name'] == 'Second action draft'
    receipt = second.fetch()
    assert receipt.ok and receipt.json()['added'] is True
    second.fulfill(response=receipt)
    expect(dialog.get_by_role('heading', name='Second action draft')).to_be_visible()
    expect(dialog.get_by_role('link', name='Open playlist')).to_be_focused()
    assert page.evaluate('window.songUpdates') == updates + ['other-song']
    assert not held
    created_names = [title for _, title, _ in song_app['main'].created]
    expected_names = ['First action mix'] if action == 'create' and outcome == 'confirmed' else []
    assert created_names == expected_names + ['Second action draft']
    assert song_app['other'].added == ([('DEST', ['owned-song'])] if action == 'add' and outcome == 'confirmed' else [])


def test_metadata_lookup_preserves_draft_and_disposes_stream_on_navigation(
        song_app, page, monkeypatch, action_evidence):
    store = song_app['store']
    tid = store.playlists.get_playlist_track_ids(song_app['source'])[0]
    release = threading.Event()
    attempts = []

    def lookup(current, tracks, _config, progress):
        attempts.append(tracks[0]['id'])
        if len(attempts) == 1:
            raise RuntimeError('internal-diagnostic-879: provider failed')
        progress({'type': 'info', 'warning': True, 'text': 'internal-diagnostic-879: provider warning'})
        assert release.wait(10)
        for provider in ('discogs', 'lastfm'):
            current.enrichment_state.log_enrichment(tid, 'lookup', provider, 'genre', 'Ambient', now=2)
        progress({'type': 'done'})

    monkeypatch.setattr('yt_playlist.providers.waterfall.run_waterfall', lookup)
    page.add_init_script('''
      window.genreStreams = [];
      const Source = window.EventSource;
      window.EventSource = class extends Source {
        constructor(url, ...args) { super(url, ...args); if (url.includes('/genre-lookup/')) window.genreStreams.push(this); }
      };
    ''')
    page.goto(f"{song_app['base']}/playlist/{song_app['source']}")
    assert page.evaluate('!!(window.originalPlayer = document.querySelector(".global-now-playing"))')
    page.get_by_role('button', name='More actions for Owned Song').click()
    page.get_by_role('menuitem', name='Edit Metadata').click()
    dialog = page.get_by_role('dialog', name='Edit Metadata')
    expect(dialog.get_by_label('Title', exact=True)).to_have_value('Owned Song')
    dialog.get_by_label('Genre', exact=True).fill('Dream Metal')
    lookup_button = dialog.get_by_role('button', name='Look up genres')
    try:
        lookup_button.click()
        expect(dialog.get_by_role('status')).to_have_text('Genre lookup did not finish. Your changes are kept.')
        expect(lookup_button).to_be_enabled()
        lookup_button.click()
        expect(dialog.get_by_role('status')).to_have_text('Some suggestions are not available yet. Checking the rest…')
        expect(dialog.get_by_label('Genre', exact=True)).to_have_value('Dream Metal')
        assert_quiet_action(page)
        release.set()
        option = dialog.get_by_role('button', name='Ambient discogs, lastfm', exact=True)
        expect(option).to_be_visible()
        expect(dialog.get_by_label('Genre', exact=True)).to_have_value('Dream Metal')
        option.click()
        expect(dialog.get_by_label('Genre', exact=True)).to_have_value('Ambient')
        page.screenshot(path=action_evidence + '-metadata-suggestions.png')
        release.clear()
        lookup_button.click()
        expect(dialog.get_by_role('status')).to_have_text('Some suggestions are not available yet. Checking the rest…')
        page.evaluate("window.navigation.navigate('/playlists')")
        expect(page).to_have_url(song_app['base'] + '/playlists')
        expect(dialog).not_to_be_visible()
        assert page.evaluate('window.genreStreams.length === 3 && window.genreStreams.every(stream => stream.readyState === EventSource.CLOSED)')
        assert page.evaluate('window.originalPlayer === document.querySelector(".global-now-playing")')
        assert not store.tracks.metadata_for_video('owned-song')['genre']
    finally:
        release.set()
