"""Radio tuning streams, axis edits, decay and metadata refresh."""
import json

import pytest
from playwright.sync_api import Error as PlaywrightError, expect
from tests.browser.radio_ui_support import radio_ui
from tests.visual_support import capture




@pytest.mark.parametrize("width", [1440])
@pytest.mark.parametrize("selection", ["stream"])
def test_tune_streams_modal_applies_all_axes_and_keeps_selected_stream_lit(page, radio_ui, width, selection):
    api = radio_ui
    page.set_viewport_size({"width": width, "height": 1000})
    api.state = {**api.ready, "tuning": {"comfort": .3, "theme": .7, "energy": .2}}
    expect(page.locator(".radio-session")).to_be_visible(timeout=6000)
    selected = page.locator(".radio-direction.is-active")
    before = selected.evaluate("el => getComputedStyle(el).boxShadow")
    assert before != "none"
    selected.hover()
    selected.evaluate("el => Promise.all(el.getAnimations().map(a => a.finished))")
    assert selected.evaluate("el => getComputedStyle(el).boxShadow") == before
    button = page.get_by_role("button", name="Tune streams", exact=True)
    expect(button).to_have_text("")
    expect(button).to_have_css("width", "56px")
    expect(button).to_have_css("height", "56px")
    expect(button.locator("svg")).to_have_css("width", "28px")
    expect(button.locator("svg")).to_have_css("height", "28px")
    expect(button).to_have_css("border-radius", "50%")
    expect(button).to_have_css("background-color", "rgba(0, 0, 0, 0)")
    expect(button).to_have_css("z-index", "0")
    backing = page.locator(".radio-junction-backing")
    expect(backing).to_have_css("fill-opacity", "1")
    assert backing.evaluate("el => getComputedStyle(el).fill") == page.locator("body").evaluate("el => getComputedStyle(el).backgroundColor")
    button.hover()
    expect(button).to_have_css("z-index", "2")
    expect(button).to_have_css("background-color", "rgba(0, 0, 0, 0)")
    button.click()
    dialog = page.get_by_role("dialog", name="Tune streams")
    expect(dialog).to_be_visible()
    expect(dialog).to_have_css("opacity", "1")
    expect(dialog.locator("p, small, .radio-tuning-drawing")).to_have_count(0)
    comfort = dialog.get_by_role("slider", name="Discovery to Comfort")
    theme = dialog.get_by_role("slider", name="Eclectic to Themed")
    energy = dialog.get_by_role("slider", name="Chill to Full On")
    expect(comfort).to_have_value("30")
    expect(theme).to_have_value("70")
    expect(energy).to_have_value("20")
    expect(comfort).to_have_css("background-image", "none")
    assert dialog.bounding_box()["width"] <= width
    capture(page, path=f"/tmp/tuneconsole-radio-tuning-{width}.png")
    dialog.get_by_role("button", name="Reset", exact=True).click()
    expect(comfort).to_have_value("50")
    comfort.focus()
    comfort.press("Home")
    expect(comfort).to_have_value("0")
    comfort.press("End")
    expect(comfort).to_have_value("100")
    expect(comfort).to_have_css("background-image", "none")
    comfort.fill("10")
    theme.fill("90")
    energy.fill("80")
    sent, pending = [], []
    def tune(route):
        sent.append(route.request.post_data_json)
        pending.append(route)
    page.route("**/radio/tune", tune)
    dialog.get_by_role("button", name="Apply", exact=True).click()
    expect(dialog).to_be_hidden()
    streams = page.locator(".radio-directions")
    expect(streams).to_have_css("opacity", "0.45")
    expect(streams).to_have_attribute('aria-busy', 'true')
    page.screenshot(path=f'/tmp/tuneconsole-1105-tuning-pending-{selection}-20261106.png')
    expect(button).to_be_enabled()
    expect(streams.get_by_role("button").first).to_be_enabled()
    button.click()
    expect(dialog.locator('.radio-tuning-status')).to_have_text('Tuning your future songs…')
    assert sent == [{"comfort": .1, "theme": .9, "energy": .8}]
    api.state = {**api.state, "tuning": sent[-1], "menu_id": "after-tuning"}
    pending.pop().fulfill(json={"ok": True, **api.state})
    expect(dialog).to_be_hidden()
    expect(streams).to_have_css("opacity", "1")
    page.screenshot(path=f'/tmp/tuneconsole-1105-tuning-complete-{selection}-20261106.png')
    expect(page.locator(".radio-session")).to_be_visible()
    button.click()
    expect(comfort).to_have_value("10")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(button).to_be_focused()

    # A genuinely newer choice must still publish its full snapshot. Hold the
    # following poll so it cannot conceal a selection receipt being discarded.
    selections, held_status = [], []

    def select(route):
        selections.append(route.request.post_data_json)
        api.state = {**api.state, "menu_id": "after-selection", "directions": [
            {**branch, "active": branch["slot"] == 1,
             "id": "selected-" + branch["id"]} for branch in api.state["directions"]]}
        route.fulfill(json={"ok": True, **api.state})

    def hold_status(route):
        held_status.append(route)

    page.route("**/radio/select", select)
    page.route("**/radio/status", hold_status)
    try:
        branch = page.locator('.radio-direction[data-slot="1"]')
        with page.expect_request("**/radio/select"):
            if selection == "song":
                branch.locator(".radio-proposed-art").nth(1).click()
            else:
                branch.locator(".radio-direction-heading").click()
        page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).switchSending === false')
        assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).state.menu_id') == "after-selection"
        assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).state.tuning') == sent[-1]
        expect(page.locator(".radio-direction.is-active")).to_have_attribute("data-slot", "1")
        assert selections == [{"proposal_id": "direction1", "menu_id": "after-tuning",
                               **({"video_id": "radio3"} if selection == "song" else {})}]
        if selection == "song":
            assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).pendingSong.accepted')
            expect(branch.locator(".radio-song-pending")).to_contain_text("Night frequency 4")
            expect(branch.locator(".is-skipping")).to_have_count(1)
            page.evaluate('''() => window.dispatchEvent(new CustomEvent('bridge-status', {detail: {
                connected: true, now_playing: {playlist_ytm_id: 'PLRADIO', video_id: 'radio3', paused: false}}}))''')
            expect(branch.locator(".radio-song-pending")).to_be_hidden()
        button.click()
        expect(comfort).to_have_value("10")
        expect(theme).to_have_value("90")
        expect(energy).to_have_value("80")
        assert len(sent) == 1 and not api.starts and not api.stops
    finally:
        page.unroute("**/radio/status", hold_status)
        for route in held_status:
            try:
                route.fulfill(json=api.state)
            except PlaywrightError:
                pass
        page.unroute("**/radio/select", select)
