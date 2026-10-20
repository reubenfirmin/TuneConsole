from playwright.sync_api import expect

from tests.test_home_vibe import seed_sounds
from yt_playlist.rec import home_vibe, mix_tuning
from yt_playlist.util.matching import identity_key


def prepare(store):
    seed_sounds(store)
    store.set_setting("last_sync_at", "1")
    store.set_setting("onboard_dismissed", "1")
    iid = store.get_identities()[0].id
    for i, genre in enumerate(["Jazz", "Techno", "Trance", "Indie Rock", "Ambient", "Classical"]):
        tid = store.upsert_track(f"favorite-{i}", f"Favorite {i}", f"Favorite artist {i}", None, None)
        store.set_track_genre(tid, genre)
        store.set_track_year(tid, 1960 + i * 10)
        store.add_history_snapshot(iid, 1, [identity_key(f"Favorite {i}", f"Favorite artist {i}")] * 10)


def test_vibe_applies_persists_recovers_from_failure_and_resets(live_app, store, page):
    prepare(store)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(live_app)
    page.get_by_role("button", name="Tune these mixes", exact=True).click()
    dialog = page.get_by_role("dialog", name="Tune these mixes", exact=True)
    expect(dialog.get_by_role("tab", name="Vibe", exact=True)).to_have_attribute("aria-selected", "true")
    expect(dialog.get_by_role("radio", name="Take it as it comes", exact=True)).to_be_checked()
    expect(dialog.locator(".fp-vibe-card")).to_have_count(7)
    expect(dialog.get_by_role("radio")).to_have_count(7)
    gentle = dialog.get_by_role("radio", name="Keep it gentle", exact=True)
    with page.expect_response("**/home/vibe"):
        gentle.check()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-vibe", "gentle")
    expect(gentle).to_be_checked()
    expect(dialog).to_be_visible()
    expect(dialog.locator("#home-vibe-status")).to_have_text("In the mix: Keep it gentle.")
    assert store.get_setting(home_vibe.SETTING) == "gentle"
    page.reload()
    expect(dialog).to_be_visible()
    expect(gentle).to_be_checked()
    expect(dialog.get_by_role("tab", name="Vibe", exact=True)).to_have_attribute("aria-selected", "true")

    page.route("**/home/vibe", lambda route: route.fulfill(status=500, body="Unavailable"))
    dialog.get_by_role("radio", name="Pick me up", exact=True).check()
    expect(dialog.locator("#home-vibe-status")).to_have_text("Couldn’t change the vibe. Try again.")
    expect(gentle).to_be_checked()
    expect(dialog.locator(".fp-vibe-fields")).to_be_enabled()
    page.unroute("**/home/vibe")
    with page.expect_response("**/home/vibe"):
        dialog.get_by_role("radio", name="Take it as it comes", exact=True).check()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-vibe", "")
    expect(dialog.locator("#home-vibe-status")).to_have_text("In the mix: Take it as it comes.")
    gentle.check()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-vibe", "gentle")
    dialog.get_by_role("button", name="Reset mix adjustments", exact=True).click()
    expect(dialog).not_to_be_visible()
    page.get_by_role("button", name="Tune these mixes", exact=True).click()
    expect(dialog.get_by_role("radio", name="Take it as it comes", exact=True)).to_be_checked()
    assert not errors


def test_mix_tabs_fit_desktop_mobile_and_short_windows(live_app, store, page, tmp_path):
    prepare(store)
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(live_app)
    page.get_by_role("button", name="Tune these mixes", exact=True).click()
    dialog = page.get_by_role("dialog", name="Tune these mixes", exact=True)
    for width, height in [(1440, 1000), (390, 850), (320, 640), (800, 480)]:
        page.set_viewport_size({"width": width, "height": height})
        for tab in ["Vibe", "Genre focus", "Nerd out"]:
            dialog.get_by_role("tab", name=tab, exact=True).click()
            expect(dialog.get_by_role("button", name="Reset mix adjustments", exact=True)).to_be_in_viewport()
            expect(dialog.get_by_role("button", name="Close mix controls", exact=True)).to_be_in_viewport()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
            panel = dialog.locator('[role="tabpanel"]:visible')
            assert panel.evaluate("el => el.scrollWidth <= el.clientWidth")
            page.screenshot(path=tmp_path / f"tuning-{tab.lower().replace(' ', '-')}-{width}.png")
        # Native disclosures keep genre adjustments handy and eras out of the initial view.
        genres = dialog.locator("details").filter(has=page.get_by_text("Your genre balance", exact=True))
        eras = dialog.locator("details").filter(has=page.get_by_text("A little time travel", exact=True))
        expect(genres).to_have_attribute("open", "")
        expect(eras).not_to_have_attribute("open", "")
        eras.locator("summary").click()
        expect(eras.get_by_role("slider")).to_have_count(5)
        eras.locator("summary").click()
        # The genre name is a keyboard disclosure; its children keep the parent slider alignment.
        toggle = dialog.locator('.fp-genre[data-fam="techno"] .fp-genre-toggle')
        toggle.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        subgenres = dialog.locator("#" + toggle.get_attribute("aria-controls"))
        expect(subgenres).to_be_visible()
        positions = dialog.locator('.fp-genre[data-fam="techno"] .fp-slider-wrap').evaluate_all(
            "els => els.map(el => el.getBoundingClientRect().x)")
        assert max(positions) - min(positions) < 1
        panel = dialog.locator('[role="tabpanel"]:visible')
        assert panel.evaluate("el => el.scrollWidth <= el.clientWidth")
        expect(dialog.get_by_role("button", name="Reset mix adjustments", exact=True)).to_be_in_viewport()
        page.screenshot(path=tmp_path / f"tuning-subgenres-{width}.png")
        toggle.press("Space")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(subgenres).not_to_be_visible()
    # Arrow navigation visits all three tabs and wraps. Home/End remain useful.
    tab = dialog.get_by_role("tab", name="Nerd out", exact=True)
    tab.press("ArrowRight")
    expect(dialog.get_by_role("tab", name="Vibe", exact=True)).to_be_focused()
    page.keyboard.press("ArrowRight")
    expect(dialog.get_by_role("tab", name="Genre focus", exact=True)).to_be_focused()
    page.keyboard.press("End")
    expect(tab).to_be_focused()
    page.keyboard.press("Home")
    expect(dialog.get_by_role("tab", name="Vibe", exact=True)).to_be_focused()


def test_open_vibe_tab_resets_when_tuning_expires(live_app, store, page):
    prepare(store)
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.clock.install()
    page.goto(live_app)
    page.get_by_role("button", name="Tune these mixes", exact=True).click()
    dialog = page.get_by_role("dialog", name="Tune these mixes", exact=True)
    dialog.get_by_role("tab", name="Vibe", exact=True).click()
    dialog.get_by_role("radio", name="Headspace", exact=True).check()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-vibe", "focus")
    store.set_setting(mix_tuning.UPDATED_AT_SETTING, str(1 - mix_tuning.TTL_S))
    with page.expect_response("**/home/fingerprint"):
        page.clock.fast_forward(mix_tuning.TTL_S * 1000)
    expect(dialog.get_by_role("radio", name="Take it as it comes", exact=True)).to_be_checked()
    expect(page.locator("#home-mode-cards")).to_have_attribute("data-vibe", "")
    expect(dialog).to_be_visible()
