"""Radio tuning streams, axis edits, decay and metadata refresh."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect
from tests.browser.radio_ui_support import radio_ui


def test_middle_stream_transition_bends_around_fixed_control(page, radio_ui):
    from tests.template_support import web_templates
    radio_ui.playlist = web_templates().get_template('_partials/radio_playlist.html').render(
        session=radio_ui.ready, tracks=radio_ui.tracks)
    radio_ui.state = radio_ui.ready
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    second = page.locator('.radio-direction[data-slot="1"]')
    third = page.locator('.radio-direction[data-slot="2"]')
    second.hover()
    page.wait_for_timeout(1600)
    transitions = []
    for index, destination in enumerate([third, second]):
        destination.hover()
        samples = page.evaluate('''() => new Promise(resolve => {
          const model = Alpine.$data(document.querySelector('.radio-home'));
          const paths = [...document.querySelectorAll('.radio-junction-line, .radio-junction-flow')];
          const {x, y, rx, ry, insetX, insetY} = model.junctionHub;
          const frames = [];
          const sample = () => {
            let nearest = Infinity;
            for (const path of paths) {
              const length = path.getTotalLength();
              for (let step = 0; step <= 400; step++) {
                const at = path.getPointAtLength(length * step / 400);
                nearest = Math.min(nearest, Math.hypot((at.x - x) / (rx - insetX), (at.y - y) / (ry - insetY)));
              }
            }
            frames.push({nearest, x: model.junctionX, fork: model.junctionForkX,
              outline: getComputedStyle(paths[0]).d, flow: getComputedStyle(paths[1]).d});
            if (frames.length < 75) requestAnimationFrame(sample); else resolve(frames);
          };
          requestAnimationFrame(sample);
        })''')
        assert min(frame['nearest'] for frame in samples) >= .998
        assert len({round(frame['x'], 2) for frame in samples}) > 10
        assert max(abs(frame['x'] - frame['fork']) for frame in samples) > 10
        transitions.append(samples)
        page.screenshot(path=f'/tmp/tuneconsole-radio-411-organic-{index}.png')
    Path('/tmp/tuneconsole-radio-411-frames.json').write_text(json.dumps(transitions))


def test_junction_geometry_remains_continuous_around_control(page, radio_ui):
    from tests.template_support import web_templates
    radio_ui.playlist = web_templates().get_template('_partials/radio_playlist.html').render(
        session=radio_ui.ready, tracks=radio_ui.tracks)
    radio_ui.state = radio_ui.ready
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    page.locator('.radio-direction[data-slot="1"]').hover()
    page.wait_for_timeout(700)
    jumps = page.evaluate(r'''() => {
      const actual = Alpine.$data(document.querySelector('.radio-home'));
      const model = radioArtwork();
      model.artworkView = {connectedSlot: 1, directionCount: 4};
      model.queueCenters = {...actual.queueCenters};
      model.junctionHub = {...actual.junctionHub};
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      const data = (x, detail) => {
        model.junctionX = model.junctionForkX = x;
        const d = model.junctionStyle(1, detail).match(/path\("(.*)"\)/)[1];
        path.setAttribute('d', d);
        return {d, length: path.getTotalLength()};
      };
      const samples = d => {
        path.setAttribute('d', d);
        const length = path.getTotalLength(), points = [];
        for (let at = 0; at <= length; at += 1) {
          const p = path.getPointAtLength(at);
          points.push([p.x, p.y]);
        }
        return points;
      };
      const distance = (a, b) => Math.max(...a.map(p =>
        Math.sqrt(Math.min(...b.map(q => (p[0]-q[0])**2 + (p[1]-q[1])**2)))));
      const jumps = [];
      for (const detail of [false, true]) {
        let previous = data(model.queueCenters[1], detail);
        for (let x = model.queueCenters[1] + .5; x <= model.queueCenters[2]; x += .5) {
          const next = data(x, detail);
          if (Math.abs(next.length - previous.length) > 10) {
            const before = samples(previous.d), after = samples(next.d);
            const movement = Math.max(distance(before, after), distance(after, before));
            if (movement > 5) jumps.push({x, detail, movement});
          }
          previous = next;
        }
      }
      return jumps;
    }''')
    assert not jumps, jumps


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


