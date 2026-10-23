"""Radio tuning streams, axis edits, decay and metadata refresh."""
import pytest
from playwright.sync_api import expect
from tests.browser.radio_ui_support import radio_ui

@pytest.mark.parametrize("width", [1440, 390])
def test_tune_streams_modal_applies_all_axes_and_keeps_selected_stream_lit(page, radio_ui, width):
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
    page.screenshot(path=f"/tmp/tuneconsole-radio-tuning-{width}.png")
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
    expect(streams).to_have_attribute("aria-busy", "true")
    expect(streams).to_have_css("opacity", "0.4")
    expect(button).to_be_disabled()
    expect(streams.get_by_role("button").first).to_be_disabled()
    assert sent == [{"comfort": .1, "theme": .9, "energy": .8}]
    api.state = {**api.state, "tuning": sent[-1]}
    pending.pop().fulfill(json={"ok": True, **api.state})
    expect(streams).to_have_attribute("aria-busy", "false")
    expect(streams).to_have_css("opacity", "1")
    expect(page.locator(".radio-session")).to_be_visible()
    button.click()
    expect(comfort).to_have_value("10")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(button).to_be_focused()


def test_decay_and_metadata_refresh_without_switching_streams(page, radio_ui):
    api = radio_ui
    api.state = {**api.ready, "directions": [
        {**b, "decay_total": 3, "decay_remaining": 3 if b["active"] else b["slot"],
         "tracks": [{**t, "genre": "ambient", "year": "2001"} for t in b["tracks"]]}
        for b in api.ready["directions"]]}
    expect(page.locator(".radio-session")).to_be_visible(timeout=6000)
    expect(page.locator(".radio-proposed-meta")).to_have_count(0)
    bars = page.get_by_role("progressbar")
    expect(bars).to_have_count(4)
    expect(bars.nth(1)).to_have_attribute("aria-valuenow", "1")
    api.state = {**api.state, "directions": [
        {**b, "decay_remaining": 0} if b["slot"] == 1 else b for b in api.state["directions"]]}
    page.evaluate("() => Alpine.$data(document.querySelector('.radio-home')).poll()")
    expect(bars.nth(1)).to_have_attribute("aria-valuenow", "0")
    expect(page.locator(".radio-direction.is-active")).to_have_attribute("data-slot", "0")


