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


def test_nav_has_home_and_playlists(live_app, page):
    page.goto(f"{live_app}/")
    nav = page.locator("header nav")
    assert nav.get_by_role("link", name="Home").is_visible()
    assert nav.get_by_role("link", name="Playlists").is_visible()


def test_shell_comparison_switches_live_but_clusters_stays_protected(live_app, page):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{live_app}/")
    page.get_by_role("button", name="Tools").click()
    assert page.get_by_role("link", name="Setup").is_visible()
    page.get_by_role("button", name="Tools").click()
    page.get_by_role("button", name="Switch navigation layout").click()
    assert page.locator("html").get_attribute("data-shell") == "rail"
    assert page.locator("header.topbar").evaluate("el => getComputedStyle(el).position") == "fixed"
    main_box = page.locator("main").bounding_box()
    rail_box = page.locator("header.topbar").bounding_box()
    assert main_box and rail_box
    assert abs((main_box["x"] + main_box["width"] / 2) - 640) < 1
    assert main_box["x"] >= rail_box["x"] + rail_box["width"]

    page.goto(f"{live_app}/clusters")
    assert page.locator("html").get_attribute("data-shell") == "top"
    assert page.locator("header.topbar").evaluate("el => getComputedStyle(el).position") == "sticky"
