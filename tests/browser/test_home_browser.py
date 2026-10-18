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
    store.set_setting("last_sync_at", "1")
    store.set_setting("onboard_dismissed", "1")
    # Keep the browser check local: album art is incidental to this control.
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(f"{live_app}/")
    panel = page.locator("#fingerprint")
    expect(panel).to_have_class("fingerprint card is-collapsed")
    button = panel.locator("#home-make-mixes")
    expect(button).to_be_hidden()
    panel.locator(".fp-collapse").click()
    expect(page.get_by_role("tab", name="These mixes", exact=True)).to_have_attribute("aria-selected", "true")
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
    button.click()
    expect(panel.locator("dialog")).not_to_be_visible()
    after = set(page.evaluate("homeOfferedGenres()"))
    assert len(after) == 4 and not before & after
    panel.locator(".fp-collapse").click()
    expect(button).to_be_enabled()
    expect(panel.locator("#home-theme-summary")).to_be_empty()
    page.get_by_role("tab", name="Preferences", exact=True).click()
    expect(button).to_be_hidden()
    expect(panel.locator(".fp-breadth-slider")).to_be_visible()
    page.get_by_role("tab", name="Preferences", exact=True).press("ArrowLeft")
    expect(page.get_by_role("tab", name="These mixes", exact=True)).to_be_focused()
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


def test_regenerating_one_mix_updates_shuffle_request(live_app, store, page):
    import json
    from urllib.parse import parse_qs

    from playwright.sync_api import expect
    from tests.test_home_other_genres import seed_genres

    seed_genres(store)
    store.set_setting("last_sync_at", "1")
    store.set_setting("onboard_dismissed", "1")
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
    with page.expect_request("**/home/other-genres") as shuffled:
        page.get_by_role("button", name="Shuffle genres", exact=True).click()
    submitted = parse_qs(shuffled.value.post_data)["genres"][0]
    assert set(json.loads(submitted)) == expected_genres


def test_genre_draft_survives_unrelated_updates_and_failed_submission(live_app, store, page):
    from playwright.sync_api import expect
    from tests.test_home_other_genres import seed_genres

    seed_genres(store)
    store.set_setting("last_sync_at", "1")
    store.set_setting("onboard_dismissed", "1")
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(f"{live_app}/")
    page.locator("#fingerprint .fp-collapse").click()
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
    store.set_setting("last_sync_at", "1")
    store.set_setting("onboard_dismissed", "1")
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
    dialog.get_by_role("tab", name="Preferences", exact=True).click()
    preference_search = dialog.get_by_role("textbox", name="search genres to add a taste bar")
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
    store.set_setting("last_sync_at", "1")
    store.set_setting("onboard_dismissed", "1")
    iid = store.upsert_identity("main", "cred", None, True)
    tid = store.upsert_track("fav", "Favorite", "Favorite artist", None, None)
    store.set_track_genre(tid, "Techno")
    store.add_history_snapshot(iid, 1.0, [identity_key("Favorite", "Favorite artist")] * 30)
    bundles = store.get_proposals("mode_bundles")
    bundles["all"]["wheelhouse"] += [
        {"key": f"archive-{i}", "genre": f"Archive {i}"} for i in range(20)]
    store.put_proposals("mode_bundles", bundles, 1.0)
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(f"{live_app}/")
    page.get_by_role("button", name="Tune these mixes", exact=True).click()
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


def test_desktop_uses_side_rail_but_clusters_has_only_exit_control(live_app, page):
    page.set_viewport_size({"width": 1280, "height": 800})
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
    assert abs((main_box["x"] + main_box["width"] / 2) - 640) < 1
    assert main_box["x"] >= rail_box["x"] + rail_box["width"]

    page.goto(f"{live_app}/clusters")
    assert page.locator("header.topbar").evaluate("el => getComputedStyle(el).display") == "none"
    assert page.get_by_role("link", name="Back to TuneConsole").is_visible()
