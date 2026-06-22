def test_home_is_landing_with_sync_and_sections(live_app, page):
    page.goto(f"{live_app}/")
    # Home is the default tab and owns the Sync control
    assert page.get_by_role("button", name="Sync plays").is_visible()
    assert page.get_by_role("button", name="Full sync").is_visible()
    assert page.get_by_role("heading", name="More in your wheelhouse").is_visible()
    # the merged Sync card carries the status badge (no separate "Time to sync" row)
    assert page.get_by_text("Never synced").is_visible()


def test_sync_button_absent_from_playlists_tab(live_app, page):
    page.goto(f"{live_app}/playlists")
    assert page.get_by_role("button", name="Sync plays").count() == 0
    assert page.get_by_role("button", name="Full sync").count() == 0


def test_nav_has_home_and_playlists(live_app, page):
    page.goto(f"{live_app}/")
    nav = page.locator("header nav")
    assert nav.get_by_role("link", name="Home").is_visible()
    assert nav.get_by_role("link", name="Playlists").is_visible()
