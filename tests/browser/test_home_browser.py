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
