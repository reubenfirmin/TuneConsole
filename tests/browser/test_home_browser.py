import pytest
from tests.derived_rating_support import seed_dislike
from tests.test_mode_cards_route import prepare_home as prepare_current_home
from tests.home_fixture_support import browser_store as store, returning_listener
from tests.prepared_fixture_support import prepared_template, roots
from tests.visual_support import capture


def prepare_home(store, now=1):
    # These are post-unlock consumers, including recovery with fewer than four
    # current offers. Prior use keeps access; current output still drives slots.
    returning_listener(store, 'mixes', now=now)
    prepare_current_home(store, now)


@pytest.fixture(scope='module')
def genre_home_template(_pristine_current_schema):
    from tests.test_home_other_genres import seed_genres

    def seed(source):
        seed_genres(source)
        prepare_home(source, 1)
        return source.rec_proposals.get_proposals('home_prepared')

    with prepared_template(_pristine_current_schema, seed) as template:
        yield template


@pytest.fixture
def prepared_genre_app(request, store, genre_home_template):
    # Restore before the live app creates its own account, playlist and jobs.
    genre_home_template.restore(store)
    return request.getfixturevalue('live_app')


@pytest.fixture(scope='module')
def mixtape_home_template(_pristine_current_schema):
    from tests.home_prepared_support import seed

    def build(source):
        # Preserve this pair's full eight-track input; the genre consumers and
        # the smaller session Home template exercise different baselines.
        seed(source)
        prepare_home(source, 1)
        payload = source.rec_proposals.get_proposals('home_prepared')
        assert payload and len(payload['menus'][0]['protos']) == 4
        return payload

    with prepared_template(_pristine_current_schema, build) as template:
        yield template


@pytest.fixture
def prepared_mixtape_app(request, store, mixtape_home_template):
    mixtape_home_template.restore(store)
    assert not store.accounts.get_accounts()
    # The live app owns its account, playlist and jobs after the private restore.
    return request.getfixturevalue('live_app')


@pytest.fixture
def home_sibling_store(tmp_path, _pristine_current_schema):
    from tests.store_fixture_support import initialize_consumer
    from yt_playlist.core.store import Store

    # File owners use the database directory, so siblings need separate parents.
    directory = tmp_path / 'sibling'
    directory.mkdir()
    sibling = Store(directory / 'home.db')
    try:
        initialize_consumer(sibling, _pristine_current_schema)
        yield sibling
    finally:
        sibling.conn.close()


@pytest.mark.parametrize("width", [1440])
def test_revisit_full_playlist_and_track_playback(live_app, store, page, width):
    from playwright.sync_api import expect

    tracks = [store.tracks.upsert_track(
        f"quiet-{15-i}", f"Quiet song {i}", "Quiet artist", None, 180,
        thumbnail="/static/onboarding-albums.png") for i in range(15)]
    playlist = store.playlists.upsert_playlist(1, "quiet-list", "Quiet mix", 15, None, 1,
                                              thumbnail='/static/onboarding-albums.png')
    store.playlists.set_playlist_tracks(playlist, tracks)
    long_tracks = [store.tracks.upsert_track(
        f"long-quiet-{i}", f"Long quiet song {i}", 'Another artist', None, 180,
        thumbnail='/static/onboarding-mixes.png') for i in range(77)]
    long_playlist = store.playlists.upsert_playlist(1, 'long-quiet-list', '90s clubbing', 77, None, 1,
                                                   thumbnail='/static/onboarding-mixes.png')
    store.playlists.set_playlist_tracks(long_playlist, long_tracks)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    prepare_home(store, 1)
    plays = []

    def play(route):
        plays.append(route.request.post_data_json["url"])
        route.fulfill(json={"ok": True})

    page.route("**/play", play)
    page.route("**/bridge/status", lambda route: route.fulfill(json={"connected": True}))
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{live_app}/")
    page.get_by_role("tab", name="Revisit", exact=True).click()
    section = page.locator("#rediscover")
    expect(section).to_be_visible()
    rows = section.get_by_role("table", name="Quiet mix tracks").locator("tbody tr")
    expect(rows).to_have_count(15)
    assert rows.locator(".ptitle").all_text_contents() == [f"Quiet song {i}" for i in range(15)]
    expect(rows.locator(".pl-thumb")).to_have_count(15)
    expect(section.get_by_role('table', name='90s clubbing tracks').locator('tbody tr')).to_have_count(77)
    expect(section.get_by_text('mostly never played')).to_have_count(0)
    expect(section.get_by_text('rarely played', exact=False)).to_have_count(2)
    expect(section.locator('.redis-sleeve img')).to_have_count(2)
    assert section.locator('.redis-sleeve img').evaluate_all('images => images.every(img => img.complete && img.naturalWidth > 0)')
    card = rows.locator('xpath=ancestor::div[contains(@class,"redis-card")]')
    play_button = card.get_by_role('link', name='Play Quiet mix', exact=True)
    assert play_button.bounding_box()['y'] < rows.first.bounding_box()['y']
    if width == 1440:
        cards = section.locator('.redis-card').all()
        assert min(c.bounding_box()['height'] for c in cards) < max(c.bounding_box()['height'] for c in cards) / 2
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    capture(section, path=f"/tmp/tuneconsole-407-revisit-{width}.png")
    play_button.click()
    rows.first.locator(".pl-play").click()
    assert plays == ['https://music.youtube.com/playlist?list=quiet-list', "https://music.youtube.com/watch?v=quiet-15&list=quiet-list"]


@pytest.fixture
def expiring_pool_jobs(monkeypatch):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.jobs import SyncJobs

    jobs = SyncJobs(completed_limit=0)
    monkeypatch.setattr(webapp, "SyncJobs", lambda: jobs)
    return jobs


@pytest.mark.parametrize("kind,lane", [("fresh_pool", "fresh"), ("library_pool", "wheelhouse")])
def test_expired_pool_stops_polling_and_refreshes_mixes(
        expiring_pool_jobs, prepared_genre_app, store, page, kind, lane,
        genre_home_template, home_sibling_store):
    import sqlite3
    from playwright.sync_api import expect

    live_app = prepared_genre_app
    if kind == 'fresh_pool':
        template = genre_home_template
        sibling = template.restore(home_sibling_store)
        assert sibling == template.result == store.rec_proposals.get_proposals('home_prepared')
        assert template.source.accounts.get_accounts() == []
        assert len(store.accounts.get_accounts()) == 1
        assert len({template.source._evidence_key, store._evidence_key, home_sibling_store._evidence_key}) == 3
        for current in (template.source, store, home_sibling_store):
            assert current.recommendations.heads() == template.source.recommendations.heads()
            assert list(map(tuple, current.conn.execute('SELECT * FROM taste_modes ORDER BY mode_id'))) == list(map(
                tuple, template.source.conn.execute('SELECT * FROM taste_modes ORDER BY mode_id')))
            assert len(current.modes.list_modes(active_only=True)) == 6
            assert current.conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert set(template.bundles) == {'all', '_meta', '0', '1', '2', '3', '4', '5'}
        for mode in map(str, range(6)):
            assert all(len(items) == 20 for items in template.bundles[mode].values())
        destinations = [roots(target.conn) for target in (store, home_sibling_store)]
        copied = 0
        for name, directory in template.files.items():
            assert len({directory, *(owned[name] for owned in destinations)}) == 3
            for source in directory.rglob('*'):
                if source.is_file():
                    paths = [source, *(owned[name] / source.relative_to(directory) for owned in destinations)]
                    assert all(path.read_bytes() == source.read_bytes() for path in paths)
                    assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
                    copied += 1
        assert copied
        assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            with template.source.conn:
                template.source.conn.execute("INSERT INTO settings VALUES ('private','forbidden')")
        sibling['menus'].clear()
        home_sibling_store._fixture_bundle_payload['all']['wheelhouse'].clear()
        home_sibling_store.settings.set_setting('private_home', 'sibling')
        assert template.result['menus'] and store.rec_proposals.get_proposals('home_prepared')['menus']
        assert len(store._fixture_bundle_payload['all']['wheelhouse']) == 120
        assert len(template.bundles['all']['wheelhouse']) == 120
        assert template.source.settings.get_setting('private_home') is None
        assert store.settings.get_setting('private_home') is None

    job = expiring_pool_jobs.create(kind)
    job.parameters = {"include_genres": [], "avoid_genres": [], "lane": lane, "epoch": 0}
    path = f"/home/{kind.replace('_', '-')}/{job.id}"
    page.goto(f"{live_app}/playlists")
    page.evaluate("""async path => {
        const board = document.createElement('div');
        board.id = 'home-mode-cards';
        board.setAttribute('x-data', '{focus: null}');
        document.body.append(board);
        await htmx.ajax('GET', path, {target: '#home-mode-cards', swap: 'innerHTML'});
    }""", path)
    card = page.locator("#home-mode-cards .fresh-pool")
    expect(card).to_have_attribute("aria-busy", "true")
    job.done = True
    assert expiring_pool_jobs.get(job.id) is None
    with page.expect_response(lambda response: response.url.endswith(path)) as poll:
        pass
    assert poll.value.status == 200
    expect(card).to_have_attribute("aria-busy", "false")
    expect(card.get_by_role("status")).to_contain_text("no longer available")
    assert card.get_attribute("hx-get") is None
    polls = []
    page.on("request", lambda request: polls.append(request.url) if request.url.endswith(path) else None)
    with page.expect_response("**/home/refresh-cards") as refreshed:
        card.get_by_role("button", name="Refresh mixes", exact=True).click()
    assert refreshed.value.status == 200
    expect(page.locator("#home-mode-cards > .gen-card")).to_have_count(4)
    # Wait longer than the old polling interval to check HTMX removed its timer.
    page.wait_for_timeout(2200)
    assert polls == []


def test_mix_hover_crossfades_even_with_reduced_motion(prepared_genre_app, store, page):
    from playwright.sync_api import expect

    live_app = prepared_genre_app
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.mouse.move(1, 1)
    page.goto(f"{live_app}/")
    cards = page.locator("#home-mode-cards > .gen-card")
    expect(cards).to_have_count(4)
    cards.first.evaluate("""el => Promise.all(
        [...el.parentElement.querySelectorAll(':scope > .gen-card')]
            .flatMap(card => card.getAnimations()).map(animation => animation.finished))""")
    def document_bounds():
        # Hover can scroll an off-screen card into view. Compare document
        # coordinates so scrolling cannot masquerade as a card layout change.
        return cards.evaluate_all("""cards => cards.map(card => {
            const box = card.getBoundingClientRect();
            return {x: box.x + scrollX, y: box.y + scrollY,
                    width: box.width, height: box.height};
        })""")

    bounds = document_bounds()

    def mid_fade_opacities():
        return cards.evaluate_all("""async cards => {
            await new Promise(resolve => setTimeout(resolve, 65));
            return cards.map(card => Number(getComputedStyle(card).opacity));
        }""")

    for reduced_motion in ("no-preference", "reduce"):
        page.emulate_media(reduced_motion=reduced_motion)
        cards.first.hover()
        assert .42 < mid_fade_opacities()[1] < 1
        for card in cards.all()[1:]:
            expect(card).to_have_css("opacity", "0.42")

        cards.nth(1).hover()
        fading = mid_fade_opacities()
        assert .42 < fading[0] < 1  # the previous card dims gradually
        assert .42 < fading[1] < 1  # the new card brightens gradually
        expect(cards.first).to_have_css("opacity", "0.42")
        expect(cards.nth(1)).to_have_css("opacity", "1")
        assert document_bounds() == bounds

        page.mouse.move(1, 1)
        assert .42 < mid_fade_opacities()[0] < 1
        for card in cards.all():
            expect(card).to_have_css("opacity", "1")
        page.locator('#home-mode-cards').screenshot(path=f'/tmp/tuneconsole-914-hover-{reduced_motion}.png')


def test_fresh_skeleton_click_builds_selected_pool_then_shows_mix(live_app, store, page, monkeypatch, tmp_path):
    import threading
    from playwright.sync_api import expect
    from tests.test_fresh_pool import _without_fresh
    from yt_playlist.rec import fresh_pool, mode_surfaces, surfaces
    from yt_playlist.util.matching import identity_key

    _without_fresh(store)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    prepare_home(store, 1)
    release = threading.Event()
    calls, tracks = [], []
    def populate(ctx, **params):
        calls.append(params)
        params["on_progress"]("Finding fresh jazz songs…")
        release.wait(15)
        tracks.extend(surfaces.ForYouItem(f"Fresh song {i}", f"New artist {i}", "", f"new{i}", None,
                                        0, "", key=identity_key(f"Fresh song {i}", f"New artist {i}"),
                                        genre="Jazz" if i % 2 else "Techno") for i in range(12))
        for track in tracks:
            store.discovery.upsert_discovered_track(track.key, track.video_id,
                track.title, track.artist, '', None, track.genre, None, 'genre:fixture', 1)
    monkeypatch.setattr(fresh_pool, "populate", populate)
    def rank(store, now, policies):
        # Control ranking, while the real preparation owner handles snapshot
        # conflicts, policy capture and typed publication in the discovery job.
        fresh = policies.stamp([surfaces._item_to_fresh_dict(track) for track in tracks], 'fresh')
        return {'all': {'fresh': fresh}}, fresh
    monkeypatch.setattr(mode_surfaces, '_calculate_bundles', rank)
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(f"{live_app}/")
    expect(page.locator("#home-mode-cards")).to_be_visible()
    page.locator("#fingerprint .fp-collapse").click()
    panel = page.locator("#fingerprint")
    panel.get_by_role("tab", name="Genre focus", exact=True).click()
    panel.get_by_role("checkbox", name="jazz", exact=True).check()
    panel.get_by_role("checkbox", name="techno", exact=True).check()
    with page.expect_response("**/home/mix-genres"):
        panel.get_by_role("button", name="Make mixes", exact=True).click()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-requested-genres", '["jazz", "techno"]')
    card = page.locator("#gen-fresh")
    expect(card.get_by_role("button", name="Find fresh songs", exact=True)).to_be_visible()
    assert not calls
    page.set_viewport_size({"width": 1280, "height": 850})
    card.scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    capture(page, path=tmp_path / "fresh-skeleton.png")
    try:
        # Native button keyboard activation starts discovery and expands the same card.
        start = card.get_by_role("button", name="Find fresh songs", exact=True)
        start.focus()
        start.press("Enter")
        expect(card).to_have_attribute("aria-busy", "true")
        expect(card.get_by_role("status")).to_have_text("Finding fresh jazz songs…")
        assert calls[0]["include_genres"] == ["jazz", "techno"]
        expect(card.locator('.fresh-pool-tile > span').first).to_have_css("animation-name", "fresh-pool-beat")
        capture(page, path=tmp_path / "fresh-building.png")
        page.emulate_media(reduced_motion="reduce")
        # Pool builds keep their activity animation under either motion preference.
        expect(card.locator('.fresh-pool-tile > span').first).to_have_css("animation-name", "fresh-pool-beat")
        page.reload()
        expect(card).to_have_attribute("aria-busy", "true")
        assert len(calls) == 1
    finally:
        release.set()
    expect(card.locator(".gen-row")).to_have_count(12, timeout=10000)
    card.locator(".gen-poster").press("Enter")
    expect(card.get_by_role("button", name="Save and play this mix")).to_be_visible()
    capture(page, path=tmp_path / "fresh-ready.png")


def test_two_ready_mixes_show_clickable_skeletons_for_the_missing_slots(live_app, store, page, monkeypatch, tmp_path):
    import threading
    from playwright.sync_api import expect
    from tests.test_library_pool import two_ready_cards
    from yt_playlist.rec import surfaces
    from yt_playlist.util.matching import identity_key

    two_ready_cards(store, monkeypatch)
    prepare_home(store, 1)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    rows = []
    for i in range(16):
        title, artist = f"Catalog song {i}", f"Catalog artist {i}"
        tid = store.tracks.upsert_track(f"v{i}", title, artist, None, None)
        store.tracks.set_track_genre(tid, "Jazz")
        rows.append(surfaces.ForYouItem(title, artist, "", f"v{i}", None, 0, "",
                                       key=identity_key(title, artist), genre="Jazz"))
    release, started = threading.Event(), threading.Event()
    calls = []
    def rank(*args, **kwargs):
        calls.append(kwargs)
        started.set()
        release.wait(15)
        return rows
    monkeypatch.setattr(surfaces, "explore_for_you", rank)
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.set_viewport_size({"width": 1280, "height": 1000})
    page.goto(f"{live_app}/")
    board = page.locator("#home-mode-cards")
    expect(board.locator(".gen-card")).to_have_count(4)
    page.locator("#fingerprint .fp-collapse").click()
    page.get_by_role("tab", name="Genre focus", exact=True).click()
    for genre in ("jazz", "techno", "trance"):
        page.locator("#fingerprint").get_by_role("checkbox", name=genre, exact=True).check()
    with page.expect_response("**/home/mix-genres"):
        page.get_by_role("button", name="Make mixes", exact=True).click()
    expect(board).to_have_attribute("data-requested-genres", '["jazz", "techno", "trance"]')
    expect(board.locator(".gen-card-body")).to_have_count(2)
    expect(board.locator(".fresh-pool")).to_have_count(2)
    for lane in ("explore", "comfort"):
        expect(page.locator(f"#gen-{lane}")).to_have_attribute("aria-busy", "false")
    card = page.locator("#gen-explore")
    expect(card.get_by_role("button", name="Build from your catalog", exact=True)).to_be_visible()
    assert not calls
    assert card.locator('.fresh-pool-tile > span').first.evaluate("el => getComputedStyle(el).animationName") == "none"
    capture(board, path=tmp_path / "two-ready-two-skeletons.png", animations="disabled")
    try:
        card.get_by_role("button", name="Build from your catalog", exact=True).click()
        expect(card).to_have_attribute("aria-busy", "true")
        assert started.wait(1) and len(calls) == 1
        expect(card).to_have_class("gen-card card fresh-pool is-building is-focused")
        expect(card.locator('.fresh-pool-tile > span').first).to_have_css("animation-name", "fresh-pool-beat")
        expect(page.locator("#gen-comfort")).to_have_attribute("aria-busy", "false")
        page.set_viewport_size({"width": 1280, "height": 850})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        capture(card, path=tmp_path / "catalog-building.png")
    finally:
        release.set()
    expect(card.locator(".gen-row")).to_have_count(12, timeout=10000)
    expect(card.get_by_role("button", name="Save and play this mix")).to_be_visible()
    recipe = page.evaluate("JSON.parse(genRecipe('gen-explore'))")
    assert recipe["genre_request"]["include"] == ["jazz", "techno", "trance"]
    expect(page.locator("#gen-wheelhouse .gen-row")).to_have_count(12)
    expect(page.locator("#gen-fresh .gen-row")).to_have_count(12)
    assert len(calls) == 1


@pytest.mark.parametrize('width', [1440])
def test_saving_wheelhouse_keeps_all_four_mixtapes(prepared_mixtape_app, store, page, monkeypatch, width):
    import json
    from playwright.sync_api import expect
    from tests.rec.test_home_prepared import service
    from tests.conftest import FakeClient
    from yt_playlist.rec import home_prepared

    live_app = prepared_mixtape_app
    payload = store.rec_proposals.get_proposals(home_prepared.SURFACE)
    initial = home_prepared.cards(store, payload, 1, service(store))
    wheelhouse = next(p for p in initial.protos if p['lane'] == 'wheelhouse')
    add = FakeClient.add_playlist_items
    writes = []

    def add_known_videos(client, pid, videos, **kwargs):
        client._catalog.update({t['video_id']: {'videoId': t['video_id'], 'title': t['title'],
            'artists': [{'name': t['artist']}]} for t in wheelhouse['tracks']})
        result = add(client, pid, videos, **kwargs)
        writes.append((client, pid, list(videos), result))
        return result

    monkeypatch.setattr(FakeClient, 'add_playlist_items', add_known_videos)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app + '/')
    board = page.locator('#home-mode-cards')
    expect(board.locator(':scope > .gen-card')).to_have_count(4)
    expect(board.locator('#gen-wheelhouse .gen-poster')).to_be_visible()
    before = board.locator('.gen-row').all_text_contents()
    response = page.request.post(live_app + '/home/generate', form={
        'name': wheelhouse['name'], 'tracks': json.dumps(wheelhouse['tracks']),
        'recipe': json.dumps(wheelhouse['recipe'])})
    assert response.ok
    assert len(writes) == 1
    provider, generated_id, videos, result = writes[0]
    expected_videos = [track['video_id'] for track in wheelhouse['tracks']]
    assert videos == expected_videos
    assert result['status'] == 'STATUS_SUCCEEDED'
    assert [track['videoId'] for track in provider.get_playlist(generated_id)['tracks']] == expected_videos
    saved = store.playlists.get_playlist_for_account(1, generated_id)
    assert saved is not None
    assert [track[1] for track in store.playlists.get_playlist_tracks_with_meta(saved.id)] == expected_videos
    assert store.playlists.get_playlist_groups()[generated_id] == 'Generated'
    assert f'data-generated-playlist="{generated_id}"' in response.text()
    assert f'window.location.assign("/playlist/{saved.id}")' in response.text()
    assert 'genComplete(' in response.text()
    page.reload()
    expect(board.locator(':scope > .gen-card')).to_have_count(4)
    expect(board.locator('#gen-wheelhouse .gen-row')).to_have_count(len(wheelhouse['tracks']))
    assert board.locator('.gen-row').all_text_contents() == before
    expect(board.locator('#gen-wheelhouse .gen-poster')).to_be_visible()
    board.screenshot(path=f'/tmp/tuneconsole-714-saved-wheelhouse-{width}.png')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate('''
        [...document.querySelectorAll('body *')].filter(el => {
          const r=el.getBoundingClientRect(); return r.width && r.right>innerWidth+1 && getComputedStyle(el).display!=='none';
        }).slice(0,12).map(el => [el.tagName,el.className,el.getBoundingClientRect().right])''')


@pytest.mark.parametrize('width', [1440])
def test_lost_held_mix_keeps_its_slot_then_recovers_after_preparation(prepared_mixtape_app, store, page, width):
    from copy import deepcopy
    from playwright.sync_api import expect
    from tests.rec.test_home_prepared import service
    from yt_playlist.rec import home_prepared

    live_app = prepared_mixtape_app
    bundles = store._fixture_bundle_payload
    payload = store.rec_proposals.get_proposals(home_prepared.SURFACE)
    payload['menus'] = [payload['menus'][0]]
    store.rec_proposals.put_proposals(home_prepared.SURFACE, payload, 1)
    first = home_prepared.cards(store, payload, 1, service(store))
    wheelhouse = next(p for p in first.protos if p['lane'] == 'wheelhouse')
    old_keys = {t['key'] for t in wheelhouse['tracks']}
    for key in old_keys:
        seed_dislike(store, key, until=None, now=1)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(f'{live_app}/')
    board = page.locator('#home-mode-cards')
    expect(board.locator(':scope > .gen-card')).to_have_count(4)
    expect(board.locator('#gen-wheelhouse.fresh-pool')).to_be_visible()
    expect(board.get_by_role('button', name='Build more in your wheelhouse', exact=True)).to_be_visible()
    assert page.locator('#gen-more').count() == 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    capture(board, path=f'/tmp/tuneconsole-428-skeleton-{width}.png')

    replacement = deepcopy(payload)
    offer = next(p for p in replacement['menus'][0]['protos'] if p['lane'] == 'wheelhouse')
    offer['tracks'] = [t for t in bundles['all']['wheelhouse'] if t['key'] not in old_keys][:12]
    next(s for s in replacement['menus'][0]['slots'] if s['proto']['lane'] == 'wheelhouse')['proto'] = offer
    store.rec_proposals.put_proposals(home_prepared.SURFACE, replacement, 2)
    expect(board.locator('#gen-wheelhouse.fresh-pool')).to_have_count(0, timeout=8000)
    expect(board.locator('#gen-wheelhouse .gen-row')).to_have_count(12)
    expect(board.locator(':scope > .gen-card')).to_have_count(4)
    assert not old_keys.intersection(board.locator('#gen-wheelhouse .gen-row').evaluate_all(
        'rows => rows.map(row => row.dataset.key)'))
    capture(board, path=f'/tmp/tuneconsole-428-recovered-{width}.png')


def test_mixtape_home_template_keeps_private_inputs(mixtape_home_template, store, home_sibling_store):
    import sqlite3
    from yt_playlist.core import profile_files
    from yt_playlist.rec import home_prepared

    template = mixtape_home_template
    first, second = template.restore(store), template.restore(home_sibling_store)
    assert first == second == template.result
    assert set(template.bundles) >= {'all', '_meta', *map(str, range(6))}
    for mid in map(str, range(6)):
        assert set(template.bundles[mid]) == {'wheelhouse', 'explore', 'comfort', 'fresh'}
        assert all(len(items) == 8 for items in template.bundles[mid].values())
    keys = {item['key'] for items in template.bundles['all'].values() for item in items}
    assert len(keys) == 192
    profiles = template.source.musical_profiles.by_keys(keys)
    assert len(profiles) == len(keys)
    assert len({template.source._evidence_key, store._evidence_key, home_sibling_store._evidence_key}) == 3
    for target in (template.source, store, home_sibling_store):
        assert not target.accounts.get_accounts()
        assert target.recommendations.heads() == template.source.recommendations.heads()
        assert target.musical_profiles.by_keys(keys) == profiles
        assert target.rec_proposals.get_proposals(home_prepared.SURFACE) == template.result
        assert target.conn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert template.source.conn.execute('PRAGMA query_only').fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        with template.source.conn:
            template.source.settings.set_setting('private_mixtape', 'forbidden')

    destinations = [roots(target.conn) for target in (store, home_sibling_store)]
    copied = 0
    for name, directory in template.files.items():
        assert len({directory, *(owned[name] for owned in destinations)}) == 3
        for source in directory.rglob('*'):
            if source.is_file():
                paths = [source, *(owned[name] / source.relative_to(directory) for owned in destinations)]
                assert all(path.read_bytes() == source.read_bytes() for path in paths)
                assert len({(path.stat().st_dev, path.stat().st_ino) for path in paths}) == 3
                copied += 1
    assert copied

    # Challenge SQL, detached nested payloads and physical profile ownership.
    second['menus'].clear()
    home_sibling_store._fixture_bundle_payload['all']['wheelhouse'].clear()
    home_sibling_store.settings.set_setting('private_mixtape', 'sibling')
    home_sibling_store.rec_proposals.put_proposals(home_prepared.SURFACE, {}, 2)
    publication = home_sibling_store.conn.execute(
        'SELECT publication FROM recording_profile_files LIMIT 1').fetchone()[0]
    relative = profile_files.relative('recording', publication)
    expected = (profile_files.directory(template.source.conn) / relative).read_bytes()
    (profile_files.directory(home_sibling_store.conn) / relative).write_bytes(b'Private damaged profile')
    assert first == template.result
    for target in (template.source, store):
        assert target.settings.get_setting('private_mixtape') is None
        assert target.rec_proposals.get_proposals(home_prepared.SURFACE) == template.result
        assert len(target._fixture_bundle_payload['all']['wheelhouse']) == 48
        assert (profile_files.directory(target.conn) / relative).read_bytes() == expected


def test_home_is_landing_with_status_and_sections(live_app, page):
    from playwright.sync_api import expect

    page.goto(f"{live_app}/")
    # Home is the default tab. Syncing is automatic in the background now, so there is no manual
    # sync button. On this live_app (no extension, never synced) the landing is the
    # welcome; the feed and the freshness line wait for a first sync.
    assert page.get_by_role("button", name="Full sync").count() == 0
    assert page.get_by_role("button", name="Sync plays").count() == 0
    expect(page.get_by_role("heading", name="Welcome to TuneConsole")).to_be_visible()
    expect(page.get_by_role("link", name="Get the extension")).to_be_visible()
    expect(page.get_by_role('heading', name='How do you like to listen?')).to_have_count(0)
    assert page.get_by_text("Library synced").count() == 0     # no awkward "not yet" line anymore


def test_cold_preparation_status_is_visible(live_app, store, page):
    from playwright.sync_api import expect

    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    store.tracks.upsert_track('cold-song', 'Song', 'Artist', None, None)
    returning_listener(store, 'mixes')
    page.goto(live_app)
    expect(page.get_by_role('status').filter(
        has_text='Your next mixes will appear as your library is prepared.')).to_be_visible()


def test_now_playing_widget_is_global(live_app, page):
    from playwright.sync_api import expect
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True,
        "now_playing": {"title": "Global Song", "artist": "Global Artist",
                        "thumbnail": "", "likeStatus": "INDIFFERENT",
                        "video_id": "global-v", "paused": False},
        "sensor_health": {"healthy": True, "ytm_tabs": 1, "responding_tabs": 1,
                          "reinjected_tabs": 0, "error": ""},
        "radio": False, "radio_waiting": False, "radio_dual": False,
        "radio_fallback_reason": None, "radio_upcoming": [],
    }))
    page.goto(f"{live_app}/playlists")
    assert page.get_by_role("button", name="Sync plays").count() == 0
    assert page.get_by_role("button", name="Full sync").count() == 0
    expect(page.locator(".global-now-playing")).to_be_visible()
    expect(page.get_by_text("Global Song", exact=True)).to_be_visible()
    expect(page.get_by_text("Global Artist", exact=True)).to_be_visible()


def test_genre_controls_make_mixes_in_the_tabbed_configurator(prepared_genre_app, store, page, tmp_path):
    from playwright.sync_api import expect
    live_app = prepared_genre_app
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    # Keep the browser check local: album art is incidental to this control.
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(f"{live_app}/")
    panel = page.locator("#fingerprint")
    expect(panel).to_have_class("fingerprint card is-collapsed")
    button = panel.locator("#home-make-mixes")
    expect(button).to_be_hidden()
    panel.locator(".fp-collapse").click()
    expect(page.get_by_role("tab", name="Vibe", exact=True)).to_be_hidden()
    expect(page.get_by_role("tab", name="Genre focus", exact=True)).to_have_attribute("aria-selected", "true")
    expect(button).to_be_enabled()
    choices = panel.get_by_role("checkbox")
    expect(choices).to_have_count(6)
    expect(button).to_have_accessible_name("Shuffle genres")
    expect(panel.locator("#fp-mixes-panel button")).to_have_count(1)
    before = set(page.evaluate("homeOfferedGenres()"))
    assert len(before) == 4
    search = panel.get_by_role("searchbox", name="Find a genre for these mixes")
    launcher = page.get_by_role("button", name="Tune these mixes", exact=True)
    dialog = page.get_by_role("dialog", name="Tune these mixes", exact=True)
    expect(dialog).to_be_visible()
    assert dialog.evaluate("el => el.matches(':modal')")
    for _ in range(24):
        page.keyboard.press("Tab")
        assert dialog.evaluate("el => el.contains(document.activeElement)")
    for _ in range(24):
        page.keyboard.press("Shift+Tab")
        assert dialog.evaluate("el => el.contains(document.activeElement)")
    page.locator("header nav").get_by_role("link", name="Playlists", exact=True).evaluate("el => el.focus()")
    assert dialog.evaluate("el => el.contains(document.activeElement)")
    page.set_viewport_size({"width": 1280, "height": 850})
    search.fill("jazz")
    search.focus()
    expect(search).to_have_value("jazz")
    assert search.evaluate("""el => {
        const css = getComputedStyle(el);
        return css.color === getComputedStyle(el.closest('dialog')).color
            && css.outlineStyle === 'none' && css.boxShadow === 'none';
    }""")
    expect(button).to_be_in_viewport()
    capture(page, path=tmp_path / "mix-modal-1280.png")
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(launcher).to_be_focused()
    launcher.click()
    expect(dialog).to_be_visible()
    # The real backdrop must not activate an underlying card or navigation.
    page.mouse.click(2, 2)
    expect(dialog).not_to_be_visible()
    expect(launcher).to_be_focused()
    assert page.url == f"{live_app}/"
    expect(page.locator(".gen-card.is-focused")).to_have_count(0)
    launcher.click()
    search.fill("nothing-matches")
    expect(panel.get_by_text("No matching genres.", exact=True)).to_be_visible()
    search.fill("jazz")
    expect(panel.locator(".fp-genre-choice:visible")).to_have_count(1)
    jazz = panel.get_by_role("checkbox", name="jazz", exact=True)
    jazz.focus()
    jazz.press("Space")
    expect(jazz).to_be_checked()
    search.fill("")
    panel.get_by_role("checkbox", name="techno", exact=True).check()
    expect(button).to_have_accessible_name("Make mixes")
    assert set(page.evaluate("homeOfferedGenres()")) == before  # selections are a draft until applied
    jazz.focus()
    page.evaluate("document.dispatchEvent(new CustomEvent('htmx:afterSettle'))")
    expect(jazz).to_be_checked()
    expect(jazz).to_be_focused()
    expect(panel.get_by_role("checkbox", name="techno", exact=True)).to_be_checked()
    for width in (1280,):
        page.set_viewport_size({"width": width, "height": 850})
        panel.scroll_into_view_if_needed()
        expect(button).to_be_in_viewport()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        capture(page, path=tmp_path / f"mix-genres-{width}.png")
    page.route("**/home/mix-genres", lambda route: route.fulfill(status=500, body="Unavailable"))
    button.click()
    expect(page.locator("#home-theme-summary")).to_have_text("Couldn’t make your mixes. Try again.")
    expect(button).to_be_enabled()
    expect(jazz).to_be_enabled()
    expect(jazz).to_be_checked()
    expect(panel.get_by_role("checkbox", name="techno", exact=True)).to_be_checked()
    assert set(page.evaluate("homeOfferedGenres()")) == before
    page.evaluate("document.dispatchEvent(new CustomEvent('htmx:afterSettle'))")
    expect(page.locator("#home-theme-summary")).to_have_text("Couldn’t make your mixes. Try again.")
    page.unroute("**/home/mix-genres")
    button.click()
    expect(panel.locator("dialog")).not_to_be_visible()
    assert set(page.evaluate("homeOfferedGenres()")) == {"jazz", "techno"}
    panel.locator(".fp-collapse").click()
    expect(jazz).to_be_checked()
    expect(panel.get_by_role("checkbox", name="techno", exact=True)).to_be_checked()
    expect(button).to_be_enabled()
    expect(panel.locator(".fp-mix-fields")).to_be_enabled()
    # Toggling the selected chips off returns the same action to genre shuffle.
    jazz.uncheck()
    panel.get_by_role("checkbox", name="techno", exact=True).uncheck()
    expect(button).to_have_accessible_name("Shuffle genres")
    assert set(page.evaluate("homeOfferedGenres()")) == {"jazz", "techno"}
    before = set(page.evaluate("homeOfferedGenres()"))
    # HTMX restores a placeholder's old CSS classes until settling. Genre metadata
    # must already include the newly ready fourth card when the swap completes.
    page.evaluate("""() => {
        window.shuffledGenresBeforeSettle = null;
        document.addEventListener('htmx:afterSwap', event => {
            if (event.detail.xhr?.responseURL.endsWith('/home/other-genres')) {
                window.shuffledGenresBeforeSettle = homeOfferedGenres();
            }
        });
    }""")
    button.click()
    expect(panel.locator("dialog")).not_to_be_visible()
    during_swap = set(page.evaluate("window.shuffledGenresBeforeSettle"))
    assert len(during_swap) == 4 and not before & during_swap
    after = set(page.evaluate("homeOfferedGenres()"))
    assert after == during_swap
    panel.locator(".fp-collapse").click()
    expect(button).to_be_enabled()
    expect(panel.locator("#home-theme-summary")).to_be_empty()
    page.get_by_role("tab", name="Nerd out", exact=True).click()
    expect(button).to_be_hidden()
    expect(panel.locator(".fp-breadth-slider")).to_be_visible()
    preference_search = dialog.get_by_role("textbox", name="Find a genre to adjust")
    preference_search.fill("jazz")
    assert preference_search.evaluate("""el => {
        const css = getComputedStyle(el);
        return css.color === getComputedStyle(el.closest('dialog')).color
            && css.outlineStyle === 'none' && css.boxShadow === 'none'
            && css.borderTopWidth === '0px';
    }""")
    capture(page, path=tmp_path / "preferences-search.png")
    page.get_by_role("tab", name="Nerd out", exact=True).press("ArrowLeft")
    expect(page.get_by_role("tab", name="Genre focus", exact=True)).to_be_focused()
    for width in (1280,):
        page.set_viewport_size({"width": width, "height": 850})
        expect(button).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    panel.get_by_role("button", name="Close mix controls", exact=True).click()
    expect(button).to_be_hidden()

    panel.locator(".fp-collapse").click()
    page.route("**/home/other-genres", lambda route: route.fulfill(status=500, body="Unavailable"))
    button.click()
    expect(page.locator("#home-theme-summary")).to_have_text("Couldn’t make your mixes. Try again.")
    expect(button).to_be_enabled()
    expect(panel.locator("#fp-mixes-panel")).not_to_have_attribute("aria-busy", "true")
    assert set(page.evaluate("homeOfferedGenres()")) == after


def test_clear_mix_controls_resets_applied_and_draft_choices_from_either_tab(prepared_genre_app, store, page, tmp_path):
    from playwright.sync_api import expect
    from yt_playlist.rec import rec_params

    live_app = prepared_genre_app
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    store.rec_model.set_weight("genre:jazz", 1.4, now=1)
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.request.post(f"{live_app}/home/mix-genres", form={"genres": "jazz"})
    page.request.post(f"{live_app}/home/steer", form={"axis": "genre:jazz", "weight": "2"})
    page.request.post(f"{live_app}/home/breadth", form={"breadth_bias": ".5"})
    page.goto(f"{live_app}/")
    panel = page.locator("#fingerprint")
    panel.locator(".fp-collapse").click()
    panel.get_by_role("tab", name="Genre focus", exact=True).click()
    expect(panel.get_by_role("checkbox", name="jazz", exact=True)).to_be_checked()
    clear = panel.get_by_role("button", name="Reset mix adjustments", exact=True)
    expect(clear).to_be_visible()
    page.get_by_role("tab", name="Nerd out", exact=True).click()
    expect(clear).to_be_visible()
    expect(panel.locator(".fp-breadth-slider")).to_have_value("0.5")
    for width in (1280,):
        page.set_viewport_size({"width": width, "height": 850})
        expect(clear).to_be_in_viewport()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        capture(page, path=tmp_path / f"mix-reset-{width}.png")
    clear.click()
    expect(panel.locator(".fp-breadth-slider")).to_have_value("0")
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-requested-genres", "[]")
    assert store.rec_model.get_leans() == {}
    assert rec_params.get_param(store, "breadth_bias") == 0
    assert store.rec_model.get_weights(now=1)["genre:jazz"] == 1.4

    expect(panel.locator("dialog")).not_to_be_visible()
    panel.locator(".fp-collapse").click()
    expect(page.get_by_role("tab", name="Nerd out", exact=True)).to_have_attribute("aria-selected", "true")
    page.get_by_role("tab", name="Genre focus", exact=True).click()
    expect(panel.locator('input[name="genres"]:checked')).to_have_count(0)
    # Clearing also discards a draft and search text even when no choice was applied.
    search = panel.get_by_role("searchbox")
    search.fill("jazz")
    panel.get_by_role("checkbox", name="jazz", exact=True).check()
    clear.click()
    expect(panel.locator("dialog")).not_to_be_visible()
    panel.locator(".fp-collapse").click()
    expect(page.get_by_role("tab", name="Genre focus", exact=True)).to_have_attribute("aria-selected", "true")
    expect(search).to_have_value("")
    expect(panel.locator('input[name="genres"]:checked')).to_have_count(0)
    expect(panel.get_by_role("checkbox")).to_have_count(6)
    expect(panel.locator("dialog")).to_be_visible()


def test_mix_controls_automatically_reset_an_open_page_after_four_hours(prepared_genre_app, store, page):
    from playwright.sync_api import expect
    from yt_playlist.rec import mix_tuning, rec_params

    live_app = prepared_genre_app
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.clock.install()
    page.goto(f"{live_app}/")
    panel = page.locator("#fingerprint")
    panel.locator(".fp-collapse").click()
    panel.get_by_role("tab", name="Genre focus", exact=True).click()
    panel.get_by_role("checkbox", name="jazz", exact=True).check()
    panel.get_by_role("button", name="Make mixes", exact=True).click()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-requested-genres", '["jazz"]')
    panel.locator(".fp-collapse").click()
    page.get_by_role("tab", name="Nerd out", exact=True).click()
    slider = panel.locator(".fp-breadth-slider")
    with page.expect_response("**/home/breadth"):
        slider.fill("0.5")
    # fill dispatches change; wait for its swap before advancing the expiry clock.
    expect(panel.locator("#home-preference-status")).to_have_text("Freshly adjusted. Find your next mix.")
    expect(slider).to_have_value("0.5")
    # The fixture's server clock is fixed at 1; age its persisted timestamp as the browser advances.
    store.settings.set_setting(mix_tuning.UPDATED_AT_SETTING, str(1 - mix_tuning.TTL_S))
    with page.expect_response("**/home/fingerprint"):
        page.clock.fast_forward(mix_tuning.TTL_S * 1000)
    expect(slider).to_have_value("0")
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-requested-genres", "[]")
    assert store.rec_proposals.get_proposals("home_genre_request") == {}
    assert rec_params.get_param(store, "breadth_bias") == 0
    expect(panel.locator("dialog")).to_be_visible()
    page.get_by_role("tab", name="Genre focus", exact=True).click()
    expect(panel.locator('input[name="genres"]:checked')).to_have_count(0)


def test_regenerating_one_mix_updates_shuffle_request(prepared_genre_app, store, page):
    from dataclasses import asdict
    import json
    import logging
    from urllib.parse import parse_qs

    from playwright.sync_api import expect
    from yt_playlist.rec import home_cards, home_prepared
    from yt_playlist.web.jobs import SyncJobs

    live_app = prepared_genre_app
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    # Publish real menus with one distinct theme per epoch band. Randomly drawing
    # from the usual menu batch can legitimately repeat the wheelhouse genre.
    payload = store.rec_proposals.get_proposals(home_prepared.SURFACE)
    service = home_cards.HomeCards(store, SyncJobs(), logging.getLogger(__name__))
    menus = []
    for epoch, order in enumerate(((0, 1, 2, 3, 4, 5), (1, 2, 3, 4, 5, 0))):
        menu = asdict(service.cards(home_cards.CardsRequest(1, epoch=epoch, mode_order=order, record=False)))
        for proto in menu["protos"]:
            proto["experiment_id"] = home_cards.experiment_id(proto)
            proto["recipe"]["experiment_id"] = proto["experiment_id"]
        by_lane = {proto["lane"]: proto for proto in menu["protos"]}
        for slot in menu["slots"]:
            if slot["proto"]:
                slot["proto"] = by_lane[slot["proto"]["lane"]]
        menus.append(menu)
    payload["menus"] = menus
    store.rec_proposals.put_proposals(home_prepared.SURFACE, payload, 1)
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(f"{live_app}/")
    card = page.locator("#gen-wheelhouse")
    expect(card).to_be_visible()
    body = card.locator(".gen-card-body")
    before = body.get_attribute("data-genres")
    siblings = page.locator(".gen-card:not(#gen-wheelhouse) .gen-card-body").evaluate_all(
        "cards => cards.flatMap(card => JSON.parse(card.dataset.genres))")
    card.locator(".gen-poster").click()
    with page.expect_response("**/home/refresh-cards") as refreshed:
        card.get_by_role("button", name="Regenerate this mix", exact=True).click()
    incoming = page.evaluate("""text => {
        const doc = new DOMParser().parseFromString(text, 'text/html');
        const body = doc.getElementById('gen-body-wheelhouse');
        return {genres: body.dataset.genres};
    }""", refreshed.value.text())
    assert incoming["genres"] != before  # the fixture must exercise a theme change
    expect(body).to_have_attribute("data-genres", incoming["genres"])

    # Only this card was replaced, so exclude its new genres plus the unchanged siblings.
    expected_genres = set(siblings) | set(json.loads(incoming["genres"]))
    assert set(page.evaluate("homeOfferedGenres()")) == expected_genres
    card.get_by_role("button", name="Back to all mixes", exact=True).click()
    page.locator("#fingerprint .fp-collapse").click()
    page.get_by_role("tab", name="Genre focus", exact=True).click()
    with page.expect_request("**/home/other-genres") as shuffled:
        page.get_by_role("button", name="Shuffle genres", exact=True).click()
    submitted = parse_qs(shuffled.value.post_data)["genres"][0]
    assert set(json.loads(submitted)) == expected_genres


def test_quick_genres_show_favorites_and_search_reaches_the_rest(live_app, store, page):
    from playwright.sync_api import expect
    from tests.test_home_other_genres import seed_genres
    from yt_playlist.util.matching import identity_key

    bundles = seed_genres(store)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    iid = store.accounts.upsert_account("main", "cred", None, True)
    tid = store.tracks.upsert_track("fav", "Favorite", "Favorite artist", None, None)
    store.tracks.set_track_genre(tid, "Techno")
    for day in range(30):
        store.history.record_history_plays(iid, 1 - day * 86400,
            [identity_key("Favorite", "Favorite artist")], source_timezone="UTC")
    bundles["all"]["wheelhouse"] += [
        {"key": f"archive-{i}|archivist", "title": f"Archive {i}", "artist": "Archivist",
         "video_id": f"archive-{i}", "genre": f"Archive {i}"} for i in range(20)]
    from tests.recommendation_support import publish_bundles, seed_bundle_subjects
    seed_bundle_subjects(store, bundles)
    publish_bundles(store, bundles, 1)
    prepare_home(store, 1)
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(f"{live_app}/")
    page.get_by_role("button", name="Tune these mixes", exact=True).click()
    page.get_by_role("tab", name="Genre focus", exact=True).click()
    choices = page.locator(".fp-genre-choice:visible")
    expect(choices).to_have_count(12)
    expect(choices.first).to_have_text("techno")
    search = page.get_by_role("searchbox", name="Find a genre for these mixes")
    search.fill("archive 17")
    page.get_by_role("checkbox", name="archive 17", exact=True).check()
    search.fill("")
    expect(choices).to_have_count(13)  # keep a chosen genre visible outside the favorite shortcuts
    expect(page.get_by_role("checkbox", name="archive 17", exact=True)).to_be_checked()


@pytest.mark.parametrize('connected', [False, True])
def test_nav_has_home_and_playlists(live_app, store, page, connected):
    from playwright.sync_api import expect
    from tests.test_growth_destinations import publish_full_journey

    with store._lock:
        publish_full_journey(store, now=1000)
        store.settings.set_setting('last_sync_at', '1')
        store.settings.set_setting('home_format', 'mixes')
    page.route('**/bridge/status', lambda route: route.fulfill(json={
        'connected': connected, 'now_playing': None}))
    page.route('https://**', lambda route: route.fulfill(status=404))
    page.set_viewport_size({'width': 1280, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(f"{live_app}/")
    nav = page.locator("header nav")
    assert nav.get_by_role("link", name="Home").is_visible()
    assert nav.get_by_role("link", name="Playlists").is_visible()
    page.wait_for_function("connected => Alpine.$data(document.querySelector('#home')).connected === connected", arg=connected)
    tabs = page.get_by_role('tablist', name='Home areas')
    attention = tabs.get_by_role('tab', name='Needs attention', exact=True)
    tv = tabs.get_by_role('tab', name='TV', exact=True)
    parameters = page.get_by_role('button', name='Adjust TV parameters', exact=True)
    for width, height in ((1280, 900), (1600, 1100), (1920, 1080)):
        page.set_viewport_size({'width': width, 'height': height})
        tabs.get_by_role('tab', name='Your unlocks', exact=True).click()
        tabs.evaluate('''el => {
            el.scrollLeft = 0;
            document.activeElement.blur();
            window.scrollTo(0, 0);
        }''')
        tv.click()
        expect(parameters).to_be_visible()
        # Exercise pointer scrolling before keyboard traversal. Playwright's click
        # would otherwise reveal a clipped tab automatically and conceal the bug.
        tabs.hover()
        page.mouse.wheel(1000, 0)
        page.wait_for_function('''() => {
            const el = document.querySelector('.dash-nav [role=tab]:last-child');
            const r = el.getBoundingClientRect(), n = el.parentElement.getBoundingClientRect();
            return r.left >= n.left && r.right <= n.right + 1;
        }''')
        attention.click()
        expect(page.locator('[data-dash="notices"]')).to_be_visible()
        tv.click()
        scroll_before = page.evaluate('scrollY')
        for _ in range(4):
            page.keyboard.press('ArrowRight')
        expect(attention).to_be_focused()
        assert page.evaluate('scrollY') == pytest.approx(scroll_before, abs=1)
        expect(tv).to_have_attribute('aria-selected', 'true')
        focused = attention.evaluate('''el => {
            const nav = el.closest('[role=tablist]'), n = nav.getBoundingClientRect();
            const r = el.getBoundingClientRect(), style = getComputedStyle(el);
            const ring = parseFloat(style.outlineWidth) + parseFloat(style.outlineOffset);
            const range = document.createRange(); range.selectNodeContents(el.firstChild);
            const text = range.getBoundingClientRect(), y = (text.top + text.bottom) / 2;
            return {scrollLeft: nav.scrollLeft, scrollWidth: nav.scrollWidth,
                clientWidth: nav.clientWidth, focusVisible: el.matches(':focus-visible'),
                labelPainted: [text.left + 1, (text.left + text.right) / 2, text.right - 1]
                    .every(x => { const top = document.elementFromPoint(x, y); return top && el.contains(top); }),
                ringVisible: r.left - ring >= n.left && r.right + ring <= n.right
                    && r.top - ring >= n.top && r.bottom + ring <= n.top + nav.clientHeight};
        }''')
        page.screenshot(path=f'/tmp/tuneconsole-991-focus-{connected}-{width}.png', full_page=True)
        assert focused['focusVisible'] and focused['labelPainted'] and focused['ringVisible'], focused
        expect(parameters).to_be_visible()
        page.keyboard.press('Enter')
        expect(attention).to_have_attribute('aria-selected', 'true')
        expect(page.locator('[data-dash="notices"]')).to_be_visible()
        # Wrap in both directions, preserving explicit activation of the panel.
        page.keyboard.press('ArrowRight')
        expect(tabs.get_by_role('tab', name='Your unlocks', exact=True)).to_be_focused()
        page.keyboard.press('ArrowLeft')
        expect(attention).to_be_focused()
        tv.click()
        parameters.click()
        expect(page.get_by_role('dialog', name='TV parameters', exact=True)).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.get_by_role('dialog', name='TV parameters', exact=True)).to_be_hidden()
    assert not errors


def test_desktop_uses_side_rail_but_clusters_has_only_exit_control(live_app, store, page):
    from tests.onboarding_support import publish_personal_mix

    # Check the normal Home layout after setup; the arrival screen uses a wider canvas.
    store.settings.set_setting('last_sync_at', '1')
    returning_listener(store, 'mixes', now=1)
    publish_personal_mix(store, now=1)
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{live_app}/")
    tools = page.get_by_role("button", name="Tools")
    # The button is server-rendered before Alpine owns its state; wait for hydration so this tests
    # the menu rather than racing a click against the framework startup.
    page.wait_for_function("el => el.getAttribute('aria-expanded') === 'false'", arg=tools.element_handle())
    for width in (1280, 1920):
        page.set_viewport_size({"width": width, "height": 800})
        tools.click()
        page.wait_for_function("el => getComputedStyle(el).opacity === '1'", arg=page.locator(".tools-pop").element_handle())
        assert page.get_by_role("link", name="Setup").is_visible()
        tools.click()
        assert page.locator("header.topbar").evaluate("el => getComputedStyle(el).position") == "fixed"
        main_box = page.locator("main").bounding_box()
        rail_box = page.locator("header.topbar").bounding_box()
        assert main_box and rail_box
        assert abs(main_box["x"] * 2 + main_box["width"] - width) < 1
        assert main_box["width"] <= 1088
        assert main_box["x"] >= rail_box["x"] + rail_box["width"]

    page.goto(f"{live_app}/clusters")
    for width in (1280, 1920):
        page.set_viewport_size({"width": width, "height": 800})
        assert page.locator("header.topbar").evaluate("el => getComputedStyle(el).display") == "none"
        assert page.get_by_role("link", name="Back to TuneConsole").is_visible()
