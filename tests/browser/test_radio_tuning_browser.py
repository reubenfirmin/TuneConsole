"""Radio tuning streams, axis edits, decay and metadata refresh."""
import json

import pytest
from playwright.sync_api import Error as PlaywrightError, expect
from tests.browser.radio_ui_support import radio_ui
from tests.visual_support import capture


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


@pytest.mark.parametrize("width", [1440])
@pytest.mark.parametrize("selection", ["stream", "song"])
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
    expect(dialog).to_be_visible()
    expect(dialog.locator('.radio-tuning-panel')).to_have_attribute('aria-busy', 'true')
    expect(dialog.locator('.radio-tuning-status')).to_have_text('Tuning your future songs…')
    expect(dialog.get_by_role('button', name='Tuning…', exact=True)).to_be_disabled()
    capture(page, path=f'/tmp/tuneconsole-radio-tuning-pending-{selection}.png')
    streams = page.locator(".radio-directions")
    expect(streams).to_have_css("opacity", "1")
    dialog.get_by_role('button', name='Close stream tuning').click()
    expect(button).to_be_enabled()
    expect(streams.get_by_role("button").first).to_be_enabled()
    button.click()
    expect(dialog.locator('.radio-tuning-status')).to_have_text('Tuning your future songs…')
    assert sent == [{"comfort": .1, "theme": .9, "energy": .8}]
    api.state = {**api.state, "tuning": sent[-1], "menu_id": "after-tuning"}
    pending.pop().fulfill(json={"ok": True, **api.state})
    expect(dialog).to_be_hidden()
    expect(streams).to_have_css("opacity", "1")
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


@pytest.mark.parametrize('failure', ['http', 'rejected', 'invalid-body', 'stalled'])
def test_unconfirmed_tuning_keeps_values_across_reopen_reload_and_status(page, radio_ui, failure):
    api = radio_ui
    api.state = {**api.ready, 'tuning': {'comfort': .5, 'theme': .5, 'energy': .5}}
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    button = page.get_by_role('button', name='Tune streams', exact=True)
    button.click()
    dialog = page.get_by_role('dialog', name='Tune streams')
    dialog.get_by_role('slider', name='Discovery to Comfort').fill('10')
    dialog.get_by_role('slider', name='Eclectic to Themed').fill('90')
    requested = {'comfort': .1, 'theme': .9, 'energy': .5}
    requests = []

    def tune(route):
        requests.append(route)
        if failure == 'http':
            route.fulfill(status=503, body='SQL timeout: internal-diagnostic-1105')
        elif failure == 'rejected':
            route.fulfill(json={'ok': False, 'reason': 'internal-diagnostic-1105'})
        elif failure == 'invalid-body':
            route.fulfill(content_type='application/json', body='internal-diagnostic-1105')

    page.route('**/radio/tune', tune)
    dialog.get_by_role('button', name='Apply', exact=True).click()
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role('button', name='Apply', exact=True)).to_be_enabled(timeout=11000)
    expect(dialog.locator('.radio-tuning-status')).to_contain_text('Your values are kept here')
    dialog.get_by_role('button', name='Close stream tuning').click()
    assert len(requests) == 1 and requests[0].request.post_data_json == requested
    if failure == 'stalled':
        requests[0].fulfill(json={'ok': True, **api.state})
    for reload in (False, True):
        if reload:
            page.reload()
            expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
        button.click()
        expect(dialog.get_by_role('slider', name='Discovery to Comfort')).to_have_value('10')
        expect(dialog.get_by_role('slider', name='Eclectic to Themed')).to_have_value('90')
        expect(dialog.locator('.radio-tuning-status')).to_contain_text('Your values are kept here')
        expect(page.get_by_role('alert')).to_have_count(0)
        expect(page.locator('body')).not_to_contain_text('internal-diagnostic-1105')
        page.keyboard.press('Escape')
        expect(page.locator('.radio-direction-heading').first).to_be_enabled()
    assert len(requests) == 1, 'Reload and polling must never replay Apply'
    api.state = {**api.state, 'tuning': requested}
    page.evaluate('Alpine.$data(document.querySelector(".radio-home")).refresh()')
    page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).tuningIntent === null')
    button.click()
    expect(dialog.get_by_role('slider', name='Discovery to Comfort')).to_have_value('10')
    expect(dialog.locator('.radio-tuning-status')).to_be_hidden()
    assert len(requests) == 1


def test_unconfirmed_tuning_survives_reload_before_saved_playback_reconnects(page, radio_ui):
    api = radio_ui
    api.state = {**api.ready, 'tuning': {'comfort': .5, 'theme': .5, 'energy': .5}}
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    requests = []

    def unavailable(route):
        requests.append(route.request.post_data_json)
        route.fulfill(status=503)

    page.route('**/radio/tune', unavailable)
    button = page.get_by_role('button', name='Tune streams', exact=True)
    button.click()
    dialog = page.get_by_role('dialog', name='Tune streams')
    dialog.get_by_role('slider', name='Discovery to Comfort').fill('10')
    dialog.get_by_role('slider', name='Eclectic to Themed').fill('90')
    requested = {'comfort': .1, 'theme': .9, 'energy': .5}
    with page.expect_response('**/radio/tune'):
        dialog.get_by_role('button', name='Apply', exact=True).click()
    page.wait_for_function('!Alpine.$data(document.querySelector(".radio-home")).tuningBusy')
    assert page.evaluate('JSON.parse(sessionStorage.getItem("radio-tuning")).values') == requested

    # The unloaded backend projects this saved playlist as inactive before its
    # extension presence reconnects. That is not an explicit session Stop.
    api.state = {**api.state, 'active': False}
    page.reload()
    page.evaluate('Alpine.$data(document.querySelector(".radio-home")).refresh()')
    assert page.evaluate('JSON.parse(sessionStorage.getItem("radio-tuning"))?.values') == requested

    api.state = {**api.state, 'active': True}
    page.evaluate('Alpine.$data(document.querySelector(".radio-home")).refresh()')
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    button.click()
    expect(dialog.get_by_role('slider', name='Discovery to Comfort')).to_have_value('10')
    expect(dialog.get_by_role('slider', name='Eclectic to Themed')).to_have_value('90')
    assert requests == [requested], 'Reconnection must reconcile without replaying Apply'
    assert not api.stops and not api.starts


@pytest.mark.parametrize('terminal', ['stopped-snapshot', 'stop-action', 'different-session'])
def test_late_tuning_receipt_cannot_restore_a_stopped_session(page, radio_ui, terminal):
    api = radio_ui
    api.state = {**api.ready, 'tuning': {'comfort': .5, 'theme': .5, 'energy': .5}}
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    requests = []
    page.route('**/radio/tune', lambda route: requests.append(route))
    page.get_by_role('button', name='Tune streams', exact=True).click()
    dialog = page.get_by_role('dialog', name='Tune streams')
    dialog.get_by_role('slider', name='Discovery to Comfort').fill('10')
    dialog.get_by_role('button', name='Apply', exact=True).click()
    page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).tuningBusy')
    old = {**api.state, 'tuning': {'comfort': .1, 'theme': .5, 'energy': .5}}
    if terminal == 'stop-action':
        dialog.get_by_role('button', name='Close stream tuning').click()
        page.locator('.radio-menu > summary').click()
        with page.expect_response('**/radio/stop'):
            page.get_by_role('button', name='New radio', exact=True).click()
        page.wait_for_function('!Alpine.$data(document.querySelector(".radio-home")).busy')
        assert api.stops == ['POST']
    else:
        # A confirmed Stop can retain the saved playlist identity. A new
        # playlist must also fence the older receipt even while it is inactive.
        api.state = {**api.state, 'active': False, 'stopped': terminal == 'stopped-snapshot',
                     'playlist_id': api.state['playlist_id'] if terminal == 'stopped-snapshot' else 'PLNEW'}
        page.evaluate('state => Alpine.$data(document.querySelector(".radio-home")).applyState(state)', api.state)
    assert len(requests) == 1
    requests[0].fulfill(json={'ok': True, **old})
    page.wait_for_function('!Alpine.$data(document.querySelector(".radio-home")).tuningBusy')
    assert not page.evaluate('Alpine.$data(document.querySelector(".radio-home")).state.active')
    assert page.evaluate('sessionStorage.getItem("radio-tuning")') is None
    if terminal == 'different-session':
        assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).state.playlist_id') == 'PLNEW'


@pytest.mark.parametrize('selection', ['stream', 'song'])
def test_selection_while_tuning_remains_usable_and_fences_the_older_apply_receipt(page, radio_ui, selection):
    api = radio_ui
    api.state = {**api.ready, 'tuning': {'comfort': .5, 'theme': .5, 'energy': .5}}
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    tuning, statuses = [], []
    page.route('**/radio/tune', lambda route: tuning.append(route))
    page.route('**/radio/status', lambda route: statuses.append(route))
    old = dict(api.state)
    requested = {'comfort': .1, 'theme': .9, 'energy': .5}

    def select(route):
        api.state = {**api.state, 'menu_id': 'newer-selection', 'directions': [
            {**branch, 'active': branch['slot'] == 1} for branch in api.state['directions']]}
        route.fulfill(json={'ok': True, **api.state})

    page.route('**/radio/select', select)
    button = page.get_by_role('button', name='Tune streams', exact=True)
    button.click()
    dialog = page.get_by_role('dialog', name='Tune streams')
    dialog.get_by_role('slider', name='Discovery to Comfort').fill('10')
    dialog.get_by_role('slider', name='Eclectic to Themed').fill('90')
    dialog.get_by_role('button', name='Apply', exact=True).click()
    expect(dialog.locator('.radio-tuning-status')).to_have_text('Tuning your future songs…')
    dialog.get_by_role('button', name='Close stream tuning').click()
    branch = page.locator('.radio-direction[data-slot="1"]')
    try:
        with page.expect_request('**/radio/select'):
            if selection == 'song':
                branch.locator('.radio-proposed-art').nth(1).click()
            else:
                branch.locator('.radio-direction-heading').click()
        page.wait_for_function('!Alpine.$data(document.querySelector(".radio-home")).switchSending')
        assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).state.menu_id') == 'newer-selection'
        tuning[0].fulfill(json={'ok': True, **old, 'menu_id': 'older-apply', 'tuning': requested})
        page.wait_for_function('!Alpine.$data(document.querySelector(".radio-home")).tuningBusy')
        assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).state.menu_id') == 'newer-selection'
        expect(page.locator('.radio-direction.is-active')).to_have_attribute('data-slot', '1')
        if selection == 'song':
            assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).pendingSong.accepted')
        button.click()
        expect(dialog.get_by_role('slider', name='Discovery to Comfort')).to_have_value('10')
        expect(dialog.locator('.radio-tuning-status')).to_contain_text('Your values are kept here')
        assert len(tuning) == 1 and not api.starts and not api.stops
    finally:
        page.unroute('**/radio/status')
        for route in statuses:
            try:
                route.fulfill(json=api.state)
            except PlaywrightError:
                pass


def test_selection_receipt_before_apply_cannot_restore_previous_tuning(page, radio_ui):
    api = radio_ui
    api.state = {**api.ready, 'tuning': {'comfort': .5, 'theme': .5, 'energy': .5}}
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    held_selections, held_status, selections, applies = [], [], [], []
    requested = {'comfort': .1, 'theme': .9, 'energy': .5}

    def select(route):
        selections.append(route.request.post_data_json)
        # The server has already accepted the selected stream. Only its old
        # response is delayed; later Apply is allowed to tune that selection.
        api.state = {**api.state, 'directions': [
            {**branch, 'active': branch['slot'] == 1} for branch in api.state['directions']]}
        held_selections.append((route, {'ok': True, **api.state}))

    def tune(route):
        applies.append(route.request.post_data_json)
        api.state = {**api.state, 'menu_id': 'after-tuning', 'tuning': requested,
                     'directions': [{**branch, 'id': 'tuned-' + branch['id'],
                                     'tracks': api.tracks[8 + branch['slot']:11 + branch['slot']]}
                                    for branch in api.state['directions']]}
        route.fulfill(json={'ok': True, **api.state})

    def hold_status(route):
        held_status.append(route)

    page.route('**/radio/select', select)
    page.route('**/radio/tune', tune)
    try:
        with page.expect_request('**/radio/select'):
            page.locator('.radio-direction[data-slot="1"] .radio-direction-heading').click()
        button = page.get_by_role('button', name='Tune streams', exact=True)
        button.click()
        dialog = page.get_by_role('dialog', name='Tune streams')
        dialog.get_by_role('slider', name='Discovery to Comfort').fill('10')
        dialog.get_by_role('slider', name='Eclectic to Themed').fill('90')
        with page.expect_response('**/radio/tune'):
            dialog.get_by_role('button', name='Apply', exact=True).click()
        page.wait_for_function('''() => {
          const radio = Alpine.$data(document.querySelector('.radio-home'));
          return !radio.tuningBusy && radio.tuningIntent === null;
        }''')
        assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).state.tuning') == requested
        assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).switchSending')

        # A slow reconciliation read must not be needed to undo a stale write
        # receipt. Keep its response pending while inspecting rendered controls.
        page.route('**/radio/status', hold_status)
        route, old_receipt = held_selections[-1]
        with page.expect_request('**/radio/status'):
            route.fulfill(json=old_receipt)
        held_selections.pop()
        button.click()
        expect(dialog.get_by_role('slider', name='Discovery to Comfort')).to_have_value('10')
        expect(dialog.get_by_role('slider', name='Eclectic to Themed')).to_have_value('90')
        assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).state.menu_id') == 'after-tuning'
        expect(page.locator('.radio-direction.is-active')).to_have_attribute('data-slot', '1')
        assert len(selections) == 1 and applies == [requested]
        assert not api.starts and not api.stops
    finally:
        # Release every controlled response even when the negative control fails.
        # A request can already have reached its production eight-second limit.
        page.unroute('**/radio/status', hold_status)
        for route in [entry[0] for entry in held_selections] + held_status:
            try:
                route.fulfill(json={'ok': True, **api.state})
            except PlaywrightError:
                pass
        page.unroute('**/radio/select', select)
        page.unroute('**/radio/tune', tune)


def test_decay_and_metadata_refresh_without_switching_streams(page, radio_ui):
    api = radio_ui
    api.state = {**api.ready, "directions": [
        {**b, "decay_total": 3, "decay_remaining": 3 if b["active"] else b["slot"],
         "tracks": [{**t, "genre": "ambient", "year": "2001"} for t in b["tracks"]]}
        for b in api.ready["directions"]]}
    expect(page.locator(".radio-session")).to_be_visible(timeout=6000)
    expect(page.locator(".radio-proposed-meta")).to_have_count(0)
    bars = page.locator('.radio-direction').get_by_role("progressbar")
    expect(bars).to_have_count(4)
    expect(bars.nth(1)).to_have_attribute("aria-valuenow", "1")
    api.state = {**api.state, "directions": [
        {**b, "decay_remaining": 0} if b["slot"] == 1 else b for b in api.state["directions"]]}
    page.evaluate("() => Alpine.$data(document.querySelector('.radio-home')).poll()")
    expect(bars.nth(1)).to_have_attribute("aria-valuenow", "0")
    expect(page.locator(".radio-direction.is-active")).to_have_attribute("data-slot", "0")


