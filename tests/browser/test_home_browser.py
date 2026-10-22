import pytest


@pytest.fixture
def expiring_pool_jobs(monkeypatch):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.jobs import SyncJobs

    jobs = SyncJobs(completed_limit=0)
    monkeypatch.setattr(webapp, "SyncJobs", lambda: jobs)
    return jobs


@pytest.mark.parametrize("kind,lane", [("fresh_pool", "fresh"), ("library_pool", "wheelhouse")])
def test_expired_pool_stops_polling_and_refreshes_mixes(expiring_pool_jobs, live_app, page, kind, lane):
    from playwright.sync_api import expect

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


@pytest.mark.parametrize("reduced_motion", ["no-preference", "reduce"])
def test_mix_hover_crossfades_even_with_reduced_motion(live_app, store, page, reduced_motion):
    from playwright.sync_api import expect
    from tests.test_home_other_genres import seed_genres

    seed_genres(store)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    page.emulate_media(reduced_motion=reduced_motion)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.mouse.move(1, 1)
    page.goto(f"{live_app}/")
    cards = page.locator("#home-mode-cards > .gen-card")
    expect(cards).to_have_count(4)
    cards.first.evaluate("""el => Promise.all(
        [...el.parentElement.querySelectorAll(':scope > .gen-card')]
            .flatMap(card => card.getAnimations()).map(animation => animation.finished))""")
    bounds = [card.bounding_box() for card in cards.all()]

    def mid_fade_opacities():
        return cards.evaluate_all("""async cards => {
            await new Promise(resolve => setTimeout(resolve, 65));
            return cards.map(card => Number(getComputedStyle(card).opacity));
        }""")

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
    assert [card.bounding_box() for card in cards.all()] == bounds

    page.mouse.move(1, 1)
    assert .42 < mid_fade_opacities()[0] < 1
    for card in cards.all():
        expect(card).to_have_css("opacity", "1")


def test_fresh_skeleton_click_builds_selected_pool_then_shows_mix(live_app, store, page, monkeypatch, tmp_path):
    import threading
    from playwright.sync_api import expect
    from tests.test_fresh_pool import _without_fresh
    from yt_playlist.rec import fresh_pool, surfaces
    from yt_playlist.util.matching import identity_key

    _without_fresh(store)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    release = threading.Event()
    calls, tracks = [], []
    def populate(ctx, **params):
        calls.append(params)
        params["on_progress"]("Finding fresh jazz songs…")
        release.wait(15)
        tracks.extend(surfaces.ForYouItem(f"Fresh song {i}", f"New artist {i}", "", f"new{i}", None,
                                        0, "", key=identity_key(f"Fresh song {i}", f"New artist {i}"),
                                        genre="Jazz" if i % 2 else "Techno") for i in range(12))
    monkeypatch.setattr(fresh_pool, "populate", populate)
    monkeypatch.setattr(surfaces, "cold_candidates", lambda *args, **kwargs: tracks)
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
    page.set_viewport_size({"width": 390, "height": 850})
    card.scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=tmp_path / "fresh-skeleton.png")
    try:
        # Native button keyboard activation starts discovery and expands the same card.
        start = card.get_by_role("button", name="Find fresh songs", exact=True)
        start.focus()
        start.press("Enter")
        expect(card).to_have_attribute("aria-busy", "true")
        expect(card.get_by_role("status")).to_have_text("Finding fresh jazz songs…")
        assert calls[0]["include_genres"] == ["jazz", "techno"]
        expect(card.locator('.fresh-pool-tile > span').first).to_have_css("animation-name", "fresh-pool-beat")
        page.screenshot(path=tmp_path / "fresh-building.png")
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
    page.screenshot(path=tmp_path / "fresh-ready.png")


def test_two_ready_mixes_show_clickable_skeletons_for_the_missing_slots(live_app, store, page, monkeypatch, tmp_path):
    import threading
    from playwright.sync_api import expect
    from tests.test_library_pool import two_ready_cards
    from yt_playlist.rec import surfaces
    from yt_playlist.util.matching import identity_key

    two_ready_cards(store, monkeypatch)
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
    board.screenshot(path=tmp_path / "two-ready-two-skeletons.png", animations="disabled")
    try:
        card.get_by_role("button", name="Build from your catalog", exact=True).click()
        expect(card).to_have_attribute("aria-busy", "true")
        assert started.wait(1) and len(calls) == 1
        expect(card).to_have_class("gen-card card fresh-pool is-building is-focused")
        expect(card.locator('.fresh-pool-tile > span').first).to_have_css("animation-name", "fresh-pool-beat")
        expect(page.locator("#gen-comfort")).to_have_attribute("aria-busy", "false")
        page.set_viewport_size({"width": 390, "height": 850})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        card.screenshot(path=tmp_path / "catalog-building.png")
    finally:
        release.set()
    expect(card.locator(".gen-row")).to_have_count(12, timeout=10000)
    expect(card.get_by_role("button", name="Save and play this mix")).to_be_visible()
    recipe = page.evaluate("JSON.parse(genRecipe('gen-explore'))")
    assert recipe["genre_request"]["include"] == ["jazz", "techno", "trance"]
    expect(page.locator("#gen-wheelhouse .gen-row")).to_have_count(12)
    expect(page.locator("#gen-fresh .gen-row")).to_have_count(12)
    assert len(calls) == 1


def test_home_is_landing_with_status_and_sections(live_app, page):
    page.goto(f"{live_app}/")
    # Home is the default tab. Syncing is automatic in the background now, so there is no manual
    # sync button. On this live_app (no extension, never synced) the landing is the
    # connect-the-extension hero; the feed and the freshness line wait for a first sync.
    assert page.get_by_role("button", name="Full sync").count() == 0
    assert page.get_by_role("button", name="Sync plays").count() == 0
    assert page.get_by_role("heading", name="Connect the extension").is_visible()
    assert page.get_by_text("Library synced").count() == 0     # no awkward "not yet" line anymore


def test_sync_button_absent_from_playlists_tab(live_app, page):
    page.goto(f"{live_app}/playlists")
    assert page.get_by_role("button", name="Sync plays").count() == 0
    assert page.get_by_role("button", name="Full sync").count() == 0


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
    expect(page.locator(".global-now-playing")).to_be_visible()
    expect(page.get_by_text("Global Song", exact=True)).to_be_visible()
    expect(page.get_by_text("Global Artist", exact=True)).to_be_visible()


def test_genre_controls_make_mixes_in_the_tabbed_configurator(live_app, store, page, tmp_path):
    from tests.test_home_other_genres import seed_genres
    from playwright.sync_api import expect
    seed_genres(store)
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
    expect(page.get_by_role("tab", name="Vibe", exact=True)).to_have_attribute("aria-selected", "true")
    page.get_by_role("tab", name="Genre focus", exact=True).click()
    expect(button).to_be_enabled()
    choices = panel.get_by_role("checkbox")
    expect(choices).to_have_count(6)
    expect(button).to_have_accessible_name("Shuffle genres")
    expect(panel.locator("#fp-mixes-panel button")).to_have_count(1)
    before = set(page.evaluate("homeOfferedGenres()"))
    assert len(before) == 4
    search = panel.get_by_role("searchbox", name="Find a genre for these mixes")
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
    for width in (1280, 390):
        page.set_viewport_size({"width": width, "height": 850})
        panel.scroll_into_view_if_needed()
        expect(button).to_be_in_viewport()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=tmp_path / f"mix-genres-{width}.png")
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
    page.get_by_role("tab", name="Nerd out", exact=True).press("ArrowLeft")
    expect(page.get_by_role("tab", name="Genre focus", exact=True)).to_be_focused()
    for width in (1280, 390):
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


def test_clear_mix_controls_resets_applied_and_draft_choices_from_either_tab(live_app, store, page, tmp_path):
    from playwright.sync_api import expect
    from tests.test_home_other_genres import seed_genres
    from yt_playlist.rec import rec_params

    seed_genres(store)
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
    for width in (1280, 390):
        page.set_viewport_size({"width": width, "height": 850})
        expect(clear).to_be_in_viewport()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=tmp_path / f"mix-reset-{width}.png")
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


def test_mix_controls_automatically_reset_an_open_page_after_four_hours(live_app, store, page):
    from playwright.sync_api import expect
    from tests.test_home_other_genres import seed_genres
    from yt_playlist.rec import mix_tuning, rec_params

    seed_genres(store)
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
        slider.dispatch_event("change")
    expect(slider).to_have_value("0.5")
    # The fixture's server clock is fixed at 1; age its persisted timestamp as the browser advances.
    store.settings.set_setting(mix_tuning.UPDATED_AT_SETTING, str(1 - mix_tuning.TTL_S))
    with page.expect_response("**/home/fingerprint"):
        page.clock.fast_forward(mix_tuning.TTL_S * 1000)
    expect(slider).to_have_value("0")
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-requested-genres", "[]")
    assert store.rec_surface.get_proposals("home_genre_request") == {}
    assert rec_params.get_param(store, "breadth_bias") == 0
    expect(panel.locator("dialog")).to_be_visible()
    page.get_by_role("tab", name="Genre focus", exact=True).click()
    expect(panel.locator('input[name="genres"]:checked')).to_have_count(0)


def test_regenerating_one_mix_updates_shuffle_request(live_app, store, page):
    import json
    from urllib.parse import parse_qs

    from playwright.sync_api import expect
    from tests.test_home_other_genres import seed_genres

    seed_genres(store)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
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


def test_genre_draft_survives_unrelated_updates_and_failed_submission(live_app, store, page):
    from playwright.sync_api import expect
    from tests.test_home_other_genres import seed_genres

    seed_genres(store)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(f"{live_app}/")
    page.locator("#fingerprint .fp-collapse").click()
    page.get_by_role("tab", name="Genre focus", exact=True).click()
    jazz = page.get_by_role("checkbox", name="jazz", exact=True)
    jazz.check()
    jazz.focus()
    page.evaluate("document.dispatchEvent(new CustomEvent('htmx:afterSettle'))")
    expect(jazz).to_be_checked()
    expect(jazz).to_be_focused()
    before = set(page.evaluate("homeOfferedGenres()"))
    page.route("**/home/mix-genres", lambda route: route.fulfill(status=500, body="Unavailable"))
    make = page.get_by_role("button", name="Make mixes", exact=True)
    make.click()
    expect(page.locator("#home-theme-summary")).to_have_text("Couldn’t make your mixes. Try again.")
    expect(make).to_be_enabled()
    expect(jazz).to_be_enabled()
    expect(jazz).to_be_checked()
    assert set(page.evaluate("homeOfferedGenres()")) == before
    page.evaluate("document.dispatchEvent(new CustomEvent('htmx:afterSettle'))")
    expect(page.locator("#home-theme-summary")).to_have_text("Couldn’t make your mixes. Try again.")


def test_configurator_modal_blocks_background_and_keeps_focus(live_app, store, page, tmp_path):
    from playwright.sync_api import expect
    from tests.test_home_other_genres import seed_genres

    seed_genres(store)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(f"{live_app}/")
    launcher = page.get_by_role("button", name="Tune these mixes", exact=True)
    launcher.click()
    dialog = page.get_by_role("dialog", name="Tune these mixes", exact=True)
    expect(dialog).to_be_visible()
    assert dialog.evaluate("el => el.matches(':modal')")
    for _ in range(24):
        page.keyboard.press("Tab")
        assert dialog.evaluate("el => el.contains(document.activeElement)")
    for _ in range(24):
        page.keyboard.press("Shift+Tab")
        assert dialog.evaluate("el => el.contains(document.activeElement)")
    # Even a script cannot focus the navigation behind a modal dialog.
    page.locator("header nav").get_by_role("link", name="Playlists", exact=True).evaluate("el => el.focus()")
    assert dialog.evaluate("el => el.contains(document.activeElement)")
    dialog.get_by_role("tab", name="Genre focus", exact=True).click()
    search = dialog.get_by_role("searchbox")
    search.fill("jazz")
    for width in (1280, 390):
        page.set_viewport_size({"width": width, "height": 850})
        search.focus()
        expect(search).to_have_value("jazz")
        assert search.evaluate("""el => {
            const css = getComputedStyle(el);
            return css.color === getComputedStyle(el.closest('dialog')).color
                && css.outlineStyle === 'none' && css.boxShadow === 'none';
        }""")
        expect(dialog.get_by_role("button", name="Shuffle genres", exact=True)).to_be_in_viewport()
        page.screenshot(path=tmp_path / f"mix-modal-{width}.png")
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(launcher).to_be_focused()
    launcher.click()
    expect(dialog).to_be_visible()
    # Backdrop catches the click; it must not open a mix or follow a link underneath.
    page.mouse.click(2, 2)
    expect(dialog).not_to_be_visible()
    expect(launcher).to_be_focused()
    assert page.url == f"{live_app}/"
    expect(page.locator(".gen-card.is-focused")).to_have_count(0)
    launcher.click()
    dialog.get_by_role("tab", name="Nerd out", exact=True).click()
    preference_search = dialog.get_by_role("textbox", name="Find a genre to adjust")
    preference_search.fill("jazz")
    assert preference_search.evaluate("""el => {
        const css = getComputedStyle(el);
        return css.color === getComputedStyle(el.closest('dialog')).color
            && css.outlineStyle === 'none' && css.boxShadow === 'none'
            && css.borderTopWidth === '0px';
    }""")
    page.screenshot(path=tmp_path / "preferences-search.png")


def test_quick_genres_show_favorites_and_search_reaches_the_rest(live_app, store, page):
    from playwright.sync_api import expect
    from tests.test_home_other_genres import seed_genres
    from yt_playlist.util.matching import identity_key

    seed_genres(store)
    store.settings.set_setting("last_sync_at", "1")
    store.settings.set_setting("onboard_dismissed", "1")
    iid = store.identities.upsert_identity("main", "cred", None, True)
    tid = store.tracks.upsert_track("fav", "Favorite", "Favorite artist", None, None)
    store.tracks.set_track_genre(tid, "Techno")
    for day in range(30):
        store.history.record_history_plays(iid, 1 - day * 86400,
            [identity_key("Favorite", "Favorite artist")], source_timezone="UTC")
    bundles = store.rec_surface.get_proposals("mode_bundles")
    bundles["all"]["wheelhouse"] += [
        {"key": f"archive-{i}", "genre": f"Archive {i}"} for i in range(20)]
    store.rec_surface.put_proposals("mode_bundles", bundles, 1.0)
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


def test_nav_has_home_and_playlists(live_app, page):
    page.goto(f"{live_app}/")
    nav = page.locator("header nav")
    assert nav.get_by_role("link", name="Home").is_visible()
    assert nav.get_by_role("link", name="Playlists").is_visible()


@pytest.mark.parametrize("width", [1280, 1920])
def test_desktop_uses_side_rail_but_clusters_has_only_exit_control(live_app, page, width):
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(f"{live_app}/")
    tools = page.get_by_role("button", name="Tools")
    # The button is server-rendered before Alpine owns its state; wait for hydration so this tests
    # the menu rather than racing a click against the framework startup.
    page.wait_for_function("el => el.getAttribute('aria-expanded') === 'false'", arg=tools.element_handle())
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
    assert page.locator("header.topbar").evaluate("el => getComputedStyle(el).display") == "none"
    assert page.get_by_role("link", name="Back to TuneConsole").is_visible()
