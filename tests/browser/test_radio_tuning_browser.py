"""Radio tuning streams, axis edits, decay and metadata refresh."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from playwright.sync_api import Error as PlaywrightError, expect
from tests.browser.radio_ui_support import radio_ui
from tests.home_fixture_support import returning_listener
from tests.radio_session_support import completed_radio_schema, completed_radio_store, play, uncontended_poc
from tests.visual_support import capture




@pytest.mark.parametrize("width", [1440])
@pytest.mark.parametrize("selection", ["stream"])
@pytest.mark.usefixtures('unlocked_home_controls')
def test_tune_streams_modal_applies_all_axes_and_keeps_selected_stream_lit(page, radio_ui, width, selection):
    api = radio_ui
    page.set_viewport_size({"width": width, "height": 1000})
    api.state = {**api.ready, "tuning": {"comfort": .3, "theme": .7, "energy": .2}}
    page.reload()
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
    style = dialog.get_by_role("combobox", name="DJ sequencing")
    expect(comfort).to_have_value("30")
    expect(theme).to_have_value("70")
    expect(energy).to_have_value("20")
    expect(style).to_have_value("")
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
    style.select_option("warm_up")
    sent, pending = [], []
    def tune(route):
        sent.append(route.request.post_data_json)
        pending.append(route)
    page.route("**/radio/tune", tune)
    dialog.get_by_role("button", name="Apply", exact=True).click()
    expect(dialog).to_be_hidden()
    streams = page.locator(".radio-directions")
    tuning_progress = page.locator(".radio-tune.is-applying")
    fill_progress = streams.get_by_text("Adding songs…", exact=True).filter(visible=True)
    expect(tuning_progress).to_be_visible()
    expect(button).to_have_attribute("title", "Applying stream tuning…")
    assert button.evaluate("el => getComputedStyle(el, '::after').animationName") == "radio-song-wait"
    expect(page.get_by_text("Finding more songs…", exact=True)).to_have_count(0)
    expect(fill_progress).to_have_count(0)
    expect(streams).to_have_css("opacity", "1")
    capture(page, path="/tmp/tuneconsole-1356-applying.png")
    expect(button).to_be_enabled()
    expect(streams.get_by_role("button").first).to_be_enabled()
    expect(streams.locator(".radio-proposed-art:enabled")).to_have_count(12)
    button.click()
    expect(dialog.locator('.radio-tuning-status')).to_have_text('Tuning your future songs…')
    assert sent == [{"comfort": .1, "theme": .9, "energy": .8, "journey": "warm_up"}]
    api.state = {**api.state, "tuning": sent[-1], "menu_id": "after-tuning"}
    filled = api.state
    api.state = {**filled, "directions": [
        {**branch, "filling": True, "more_pending": True, "tracks": branch["tracks"][:1]}
        for branch in filled["directions"]]}
    pending.pop().fulfill(json={"ok": True, **api.state})
    expect(dialog).to_be_hidden()
    expect(tuning_progress).to_have_count(0)
    expect(fill_progress).to_have_count(4)
    expect(streams.locator(".radio-proposed-art:enabled")).to_have_count(4)
    expect(streams.locator(".radio-direction.is-active .radio-queue-status")).to_have_text("Up next")
    capture(page, path="/tmp/tuneconsole-1356-adding-songs.png")
    api.state = filled
    expect(fill_progress).to_have_count(0)
    expect(streams.locator(".radio-proposed-art")).to_have_count(12)
    page.screenshot(path=f'/tmp/tuneconsole-1105-tuning-complete-{selection}-20261106.png')
    expect(page.locator(".radio-session")).to_be_visible()
    button.click()
    expect(comfort).to_have_value("10")
    expect(style).to_have_value("warm_up")
    expect(dialog).to_have_css("opacity", "1")
    page.screenshot(path=f'/tmp/tuneconsole-radio-dj-style-{width}.png')
    dialog.get_by_role("button", name="Reset", exact=True).click()
    expect(style).to_have_value("")
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


def test_tune_streams_keeps_energy_when_earned_vibes_output_is_unavailable(page, radio_ui, store):
    returning_listener(store, 'vibes')
    radio_ui.state = {**radio_ui.ready, 'tuning': {'comfort': .3, 'theme': .7, 'energy': .2}}
    page.reload()
    expect(page.locator('.radio-session')).to_be_visible()
    with page.expect_response('**/home/growth') as growth_response:
        page.get_by_role('button', name='Tune streams', exact=True).click()
    growth = growth_response.value.json()
    assert growth['vibes_ready'] is False
    vibes = next(node for node in growth['nodes'] if node['id'] == 'vibes')
    assert vibes['unlocked'] is True
    slider = page.get_by_role('slider', name='Chill to Full On')
    expect(slider).to_be_visible()
    expect(slider).to_have_value('20')


@pytest.mark.parametrize("partial", [False, True], ids=["full", "one-short-stream"])
def test_stream_feedback_after_backend_restart(page, radio_ui, uncontended_poc, store, monkeypatch, partial):
    controller, provider, frames, _, _ = uncontended_poc
    assert controller.start()["ok"]
    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(play, controller, "v2").result(timeout=5)
    controller.suspend()
    directions = controller.session.directions
    complete = directions.slots[-1]["tracks"]
    assert [len(branch["tracks"]) for branch in directions.slots] == [6] * 4
    directions.filling = True
    if partial:
        directions.slots[-1]["tracks"] = complete[:1]
    controller._persist(controller.session)
    previous = list(provider.ids), list(provider.creations), list(provider.adds), list(provider.removes), list(frames)

    restarted = type(controller)(controller.ctx)
    controller.ctx.radio = restarted
    monkeypatch.setattr(restarted, "_start_monitor", lambda session: None)
    bootstrap = type(controller).bootstrap

    def home_bootstrap(radio, *, reader=None):
        if radio.ctx.store is store:
            return bootstrap(restarted)
        return bootstrap(radio, reader=reader)

    monkeypatch.setattr(type(controller), "bootstrap", home_bootstrap)
    # Hold the live status response to check the server-rendered checkpoint first.
    statuses = []
    page.route("**/radio/status", lambda route: statuses.append(route))
    try:
        page.reload()
        session = page.locator(".radio-session")
        expect(session).to_be_visible()
        assert restarted.session is None
        expect(session.locator('tr[data-vid="v2"]')).to_be_visible()
        expect(session.locator(".radio-direction")).to_have_count(4)
        expect(session.locator(".radio-proposed-art:enabled")).to_have_count(19 if partial else 24)
        expect(session.locator(".radio-direction.is-active .radio-queue-status")).to_have_text("Up next")
        fill = session.locator(".radio-direction-fill:visible")
        expect(fill).to_have_count(int(partial))
        if partial:
            expect(session.locator('[data-slot="3"] .radio-direction-fill')).to_be_visible()
        expect(session.locator(".radio-tune.is-applying")).to_have_count(0)
        expect(page.get_by_text("Finding more songs…", exact=True)).to_have_count(0)

        # A live poll still carries the generic refill flag. An unrelated
        # tuning status must not put this tab's Tune streams control in progress.
        live = restarted.join()
        assert live["active"] and live["position"] == 1 and live["playlist_id"] == "PLSESSION"
        assert all(branch["filling"] for branch in live["directions"])
        page.unroute("**/radio/status")
        radio_ui.state = {**live, "tuning_pending": {"comfort": .1, "theme": .5, "energy": .5}}
        page.route("**/radio/status", lambda route: route.fulfill(json=radio_ui.state))
        if statuses:
            statuses.pop().fulfill(json=radio_ui.state)
        page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).state.tuning_pending?.comfort === .1')
        expect(fill).to_have_count(int(partial))
        expect(session.locator(".radio-tune.is-applying")).to_have_count(0)
        capture(page, path=f"/tmp/tuneconsole-1356-restored-{'partial' if partial else 'full'}.png")

        # Even before the plan-wide flag clears, a completed card is usable.
        with restarted.lock:
            restarted.session.directions.slots[-1]["tracks"] = complete
        radio_ui.state = restarted.status()
        expect(session.locator(".radio-proposed-art:enabled")).to_have_count(24)
        expect(fill).to_have_count(0)
        expect(session.locator('tr[data-vid="v2"]')).to_be_visible()
        assert (provider.ids, provider.creations, provider.adds, provider.removes, frames) == previous
        assert not radio_ui.starts and not radio_ui.stops
    finally:
        restarted.work.stop_executor()
