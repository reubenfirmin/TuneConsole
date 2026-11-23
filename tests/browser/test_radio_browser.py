"""Radio's launch, playback-presence gaps, and launcher interactions in the real UI."""
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from sqlite3 import OperationalError

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.template_support import web_templates
from playwright.sync_api import expect
from tests.browser.radio_ui_support import radio_ui
from tests.radio_session_support import (
    completed_radio_schema, completed_radio_store, play, replace_native_future, uncontended_poc,
)
from yt_playlist.core.bridge import PreparedNavigation
from yt_playlist.rec import choice_evidence
from yt_playlist.web.routes.radio_session import build as radio_routes


def test_delayed_native_transition_and_successor_appear_in_desktop_history(page, radio_ui, uncontended_poc):
    controller, remote, *_ = uncontended_poc
    clock = [1234.0]
    controller.ctx.now_fn = controller.listening.now_fn = lambda: clock[0]
    controller.ctx.templates = web_templates()
    app = FastAPI()
    app.include_router(radio_routes(controller.ctx))

    with ThreadPoolExecutor(max_workers=1) as worker, TestClient(app) as client:
        successor = worker.submit(replace_native_future, controller, remote).result()

        def playlist(route):
            response = client.get("/radio/playlist")
            route.fulfill(status=response.status_code, content_type="text/html", body=response.text)

        page.route("**/radio/playlist", playlist)
        radio_ui.state = controller.status()
        rows = page.locator(".radio-session table tbody tr[data-vid]")
        expect(rows).to_have_count(3, timeout=6000)

        clock[0] += 165
        worker.submit(play, controller, "v3").result()
        history = ["v1", "v2", "v3"]
        for song in ("v4", successor, "v4"):
            worker.submit(play, controller, song).result()
            radio_ui.state = controller.status()
            page.evaluate("() => Alpine.$data(document.querySelector('.radio-home')).poll()")
            history.append(song)
            expect(rows).to_have_count(len(history), timeout=6000)
            assert rows.evaluate_all("rows => rows.map(row => row.dataset.vid)") == history
            expect(rows.last).to_have_attribute("aria-current", "true")
            expect(page.locator(".radio-session tr[aria-current=true]")).to_have_count(1)
            if len(history) == 4:
                controller.ctx.bridge.auto_adopt = True
                worker.submit(play, controller, "v4").result()
                assert controller.status()["browser"]["playback_synced"]
        page.screenshot(path="/tmp/tuneconsole-1372-desktop-history.png")






@pytest.mark.parametrize(('entry', 'confirmation'), [('hash', 'bridge'), ('direct', 'status'), ('tab', 'status')])
def test_launch_reveals_stream_slots_at_playback_while_history_is_pending(page, radio_ui, live_app, entry, confirmation):
    api = radio_ui
    if entry == 'direct':
        page.goto(f'{live_app}/?panel=radio')
    if entry == 'tab':
        page.goto(f'{live_app}/?panel=directions')
        expect(page.get_by_role('tab', name='Mix tapes', exact=True)).to_have_attribute('aria-selected', 'true')
        page.get_by_role('tab', name='On air', exact=True).click()
    page.locator(".radio-seed").first.click()
    launch = page.get_by_role("status", name="Starting radio")
    expect(launch).to_be_visible()
    expect(launch).to_contain_text("Finding your frequency")
    expect(page.locator(".radio-seeds")).to_be_hidden()
    expect(page.locator(".radio-session")).to_be_hidden()
    expect(launch.locator(".gen-node")).to_have_count(8)
    expect(launch).to_have_css("opacity", "1")
    expect(launch.locator("button:visible, a:visible")).to_have_count(0)
    page.screenshot(path="/tmp/tuneconsole-radio-launch.png")

    api.state = {**api.ready, "starting": True, "tracks": api.tracks[:1], "local_id": None,
                 "directions": [], "position": -1}
    api.hold_playlist = True
    assert len(api.starts) == 1
    assert api.starts[0].request.post_data_json["video_id"] == "radio0"
    api.starts.pop().fulfill(json={"ok": True, **api.state})
    expect(launch).to_contain_text("Connecting to YouTube Music")
    expect(launch.locator("button:visible, a:visible")).to_have_count(0)
    session = page.locator(".radio-session")
    expect(session).to_be_hidden()
    if confirmation == "bridge":
        page.evaluate('''song => window.dispatchEvent(new CustomEvent('bridge-status', {
          detail: {connected: true, now_playing: {...song, playlist_ytm_id: 'PLRADIO', paused: false}},
        }))''', api.tracks[0])
    else:
        api.state = {**api.state, "starting": False, "position": 0}
    expect(session).to_be_visible(timeout=6000)
    expect(launch).to_be_hidden()
    expect(session.locator("table")).to_have_count(0)
    slots = session.locator(".radio-direction")
    expect(slots).to_have_count(4)
    expect(session.get_by_text("Filling in…", exact=True)).to_have_count(4)
    expect(slots.first.locator(".radio-proposed-track")).to_have_count(0)
    if confirmation == "bridge":
        with page.expect_response("**/radio/status", timeout=6000):
            pass
        expect(session).to_be_visible()
        expect(launch).to_be_hidden()
        page.screenshot(path="/tmp/tuneconsole-radio-streams-filling.png", animations="allow")

    api.state = {**api.state, "starting": False, "position": 0, "tracks": api.tracks[:3], "local_id": 1}
    expect(slots.first.locator(".radio-proposed-track")).to_have_count(2)
    expect(slots.first.locator(".radio-proposed-art").first).to_be_disabled()
    expect(launch).to_be_hidden()

    # A new menu can arrive while the original history request is still open.
    api.state = api.ready
    expect(session.get_by_text("Filling in…", exact=True)).to_have_count(0, timeout=6000)
    expect(slots.nth(1).locator(".radio-direction-heading")).to_be_enabled()
    expect(session.locator("table")).to_have_count(0)
    api.hold_playlist = False
    for route in api.playlists:
        route.fulfill(content_type="text/html", body=api.playlist)
    expect(session.locator("table")).to_be_visible(timeout=6000)
    expect(launch).to_be_hidden()


def test_empty_seed_offer_recovers_while_on_air_stays_open(page, radio_ui, live_app, store):
    store.settings.set_setting('radio_seed_offer', '{"tracks": [], "menu_id": null}')
    page.add_init_script("sessionStorage.removeItem('radio-view')")
    requests = []

    def seeds(route):
        requests.append(route.request)
        if len(requests) == 1:
            route.fulfill(json={'tracks': [], 'menu_id': None})
        else:
            tracks = [dict(track, thumbnail=None) for track in radio_ui.tracks]
            route.fulfill(json={'tracks': tracks, 'menu_id': 'seeds'})

    page.route('**/radio/seeds', seeds)
    page.goto(f'{live_app}/?panel=directions')
    page.get_by_role('tab', name='On air', exact=True).click()
    expect(page.get_by_role('tab', name='On air', exact=True)).to_have_attribute('aria-selected', 'true')
    expect(page.get_by_text('Starter songs aren’t available yet.')).to_be_visible()
    expect(page.locator('.radio-seed-start').first).to_be_visible(timeout=12000)
    expect(page.locator('.radio-seed-info').first).to_have_css('opacity', '1')
    expect(page.locator('.radio-seed-info strong').first).to_have_text(radio_ui.tracks[0]['title'])
    expect(page.get_by_text('Starter songs aren’t available yet.')).to_be_hidden()
    assert len(requests) == 2


def test_radio_seed_exposure_requires_positive_visible_area(page, radio_ui, live_app, store):
    menu = 'radio-picker-exposure'
    choices = [{'id': song['video_id'], 'position': i, 'tracks': [song], 'intent': 'seed'}
               for i, song in enumerate(radio_ui.tracks, start=1)]
    choice_evidence.issue(store, 'radio', choices, 1, context={}, opportunity_id=menu)
    offer = {'tracks': radio_ui.tracks, 'menu_id': menu}
    store.settings.set_setting('radio_seed_offer', json.dumps(offer))
    page.route('**/radio/seeds', lambda route: route.fulfill(json=offer))
    page.add_init_script('''document.addEventListener('DOMContentLoaded', () => {
      const style = document.createElement('style');
      style.textContent = '.radio-seed:first-of-type .radio-seed-start { height: 0 !important; }';
      document.head.append(style);
    });''')
    reported = []

    def report(request):
        if request.url.endswith('/home/listening-response'):
            data = request.post_data_json
            if data['opportunity_id'] == menu and data['kind'] == 'visible':
                reported.append(data['option_id'])

    page.on('request', report)
    with page.expect_response(lambda response: response.url.endswith('/home/listening-response')
                              and response.request.post_data_json['option_id'] == 'radio1') as exposure:
        page.goto(f'{live_app}/?panel=directions')
        page.get_by_role('tab', name='On air', exact=True).click()
    assert exposure.value.status == 204
    seeds = page.locator('.radio-seed-start')
    expect(seeds.nth(1)).to_be_visible()
    assert seeds.first.evaluate('''el => new Promise(resolve => {
      const observer = new IntersectionObserver(([entry]) => {
        observer.disconnect();
        resolve([entry.isIntersecting, entry.intersectionRatio, entry.intersectionRect.height]);
      });
      observer.observe(el);
    })''') == [True, 1, 0]
    assert 'radio1' in reported and 'radio0' not in reported




@pytest.mark.parametrize('interruption', ['network', 'provider'])
def test_interrupted_start_retries_same_selection_after_reload(page, radio_ui, interruption):
    with page.expect_request("**/radio/start"):
        page.locator(".radio-seed-start").first.click()
    first = radio_ui.starts[0].request.post_data_json
    if interruption == 'network':
        radio_ui.starts[0].abort("failed")
    else:
        radio_ui.starts[0].fulfill(json={"ok": False, "launch_outcome": "uncertain"})

    recovery = page.get_by_role("status", name="Starting radio")
    retry = recovery.get_by_role("button", name="Try this selection again")
    expect(retry).to_be_visible()
    page.reload()
    check = recovery.get_by_role("button", name="Check playback")
    with page.expect_response("**/radio/status"):
        check.click()
    expect(retry).to_be_visible()
    assert len(radio_ui.starts) == 1

    with page.expect_request("**/radio/start"):
        retry.click()
    assert len(radio_ui.starts) == 2
    second = radio_ui.starts[1].request.post_data_json
    assert {**second, "clicked_at_ms": first["clicked_at_ms"]} == first
    radio_ui.state = {**radio_ui.ready, "launch_id": first["launch_id"]}
    radio_ui.starts[1].fulfill(json={"ok": True, **radio_ui.state})
    expect(page.locator(".radio-session")).to_be_visible()
    expect(recovery).to_be_hidden()
    assert page.evaluate('sessionStorage.getItem("radio-launch")') is None


@pytest.mark.parametrize('first_stop', ['confirmed', 'lost-response', 'failed-write'])
def test_uncertain_launch_can_choose_another_song(page, radio_ui, uncontended_poc, monkeypatch, first_stop):
    controller, provider, frames, _, _ = uncontended_poc
    ctx = controller.ctx
    ctx.store.settings.set_setting('onboard_listening_format', 'radio')
    offer = controller.seeds()
    checkpoints = ctx.store.radio_checkpoints
    starts, stops = [], []

    class ApplicationExited(BaseException):
        pass

    def exit_before_queueing(frame, *, intent):
        assert isinstance(frame, PreparedNavigation) and frame.prepare()
        raise ApplicationExited

    def fail_stop_write(_launch_id):
        raise OperationalError('Stop write interrupted')

    with ExitStack() as clients:
        client = None

        def start(route):
            nonlocal client
            request = route.request.post_data_json
            starts.append(request)
            if len(starts) == 1:
                with monkeypatch.context() as patch:
                    patch.setattr(ctx.bridge, 'send_control', exit_before_queueing)
                    with controller.launch_request(request['launch_id']) as launch, pytest.raises(ApplicationExited):
                        controller.start(request['video_id'], menu_id=request['menu_id'], launch=launch)
                receipt = checkpoints.pending_seeded_launch()
                assert receipt['dispatch_started'] and receipt['playlist'] == 'PLSESSION'
                ctx.radio = reopened = type(controller)(ctx)
                clients.callback(reopened.work.stop_executor)
                app = FastAPI()
                app.include_router(radio_routes(ctx))
                client = clients.enter_context(TestClient(app, raise_server_exceptions=False))
                route.abort('failed')
                return
            reply = client.post('/radio/start', json=request)
            route.fulfill(status=reply.status_code, json=reply.json())

        def stop(route):
            body = route.request.post_data_json
            stops.append(body)
            with monkeypatch.context() as patch:
                if len(stops) == 1 and first_stop == 'failed-write':
                    patch.setattr(checkpoints, 'take_seeded_launch', fail_stop_write)
                reply = client.post('/radio/stop', json=body)
            if len(stops) == 1 and first_stop == 'lost-response':
                assert reply.json()['ok'] is True
                route.abort('failed')
            else:
                route.fulfill(status=reply.status_code, body=reply.text, headers=dict(reply.headers))

        for path in ('seeds', 'status', 'start', 'stop'):
            page.unroute(f'**/radio/{path}')
        page.route('**/radio/seeds', lambda route: route.fulfill(json=offer))
        page.route('**/radio/status', lambda route: route.fulfill(
            json=client.get('/radio/status').json() if client is not None else {'active': False}))
        page.route('**/radio/start', start)
        page.route('**/radio/stop', stop)
        page.reload()

        first, second = [page.get_by_role('button', name=f"Start radio with {song['title']} by {song['artist']}")
                         for song in offer['tracks'][:2]]
        first.click()
        retry = page.get_by_role('button', name='Try this selection again')
        expect(retry).to_be_visible()
        page.reload()
        with page.expect_response('**/radio/start') as response:
            retry.click()
        assert response.value.json()['launch_outcome'] == 'uncertain'
        with page.expect_response('**/radio/status'):
            page.get_by_role('button', name='Check playback').click()
        launch_id = starts[0]['launch_id']
        assert [request['launch_id'] for request in starts] == [launch_id, launch_id]
        assert len(provider.creations) == 1 and not frames

        choose_another = page.get_by_role('button', name='Choose another song')
        expect(second).to_be_hidden()
        with page.expect_request('**/radio/stop'):
            choose_another.click()
        expected_stops = [{'launch_id': launch_id}]
        if first_stop != 'confirmed':
            expect(choose_another).to_be_visible()
            expect(second).to_be_hidden()
            assert page.evaluate('JSON.parse(sessionStorage.getItem("radio-launch")).id') == launch_id
            assert (checkpoints.pending_seeded_launch() is None) == (first_stop == 'lost-response')
            assert len(stops) == 1 and not frames
            page.reload()
            choose_another.click()
            expected_stops.append({'launch_id': launch_id})
        expect(second).to_be_visible()
        expect(first).to_be_focused()
        assert stops == expected_stops and checkpoints.pending_seeded_launch() is None
        assert page.evaluate('sessionStorage.getItem("radio-launch")') is None
        assert len(starts) == 2 and len(provider.creations) == 1 and not frames

        page.reload()
        with page.expect_response('**/radio/start') as response:
            second.click()
        assert response.value.json()['ok'] is True
        assert len(starts) == 3 and starts[-1]['launch_id'] != launch_id
        seed = offer['tracks'][1]['video_id']
        assert len(provider.creations) == 2 and provider.ids == [seed]
        assert len(frames) == 1 and f'v={seed}&list=PLSESSION' in frames[0]['url']


def test_choose_another_song_focuses_picker_after_delayed_reveal(page, radio_ui):
    first = page.locator('.radio-seed-start').first
    with page.expect_request('**/radio/start'):
        first.press('Enter')
    radio_ui.starts[0].abort('failed')
    choose_another = page.get_by_role('button', name='Choose another song')
    expect(choose_another).to_be_visible()
    page.evaluate('''() => {
      const grid = document.querySelector('.radio-seeds');
      const reveal = grid._x_doShow;
      grid._x_doShow = () => { window.revealRadioSeeds = reveal; };
    }''')

    with page.expect_response('**/radio/stop'):
        choose_another.press('Enter')
    page.wait_for_function("typeof window.revealRadioSeeds === 'function'")
    page.evaluate('() => Alpine.nextTick()')
    expect(first).to_be_hidden()
    expect(first).not_to_be_focused()
    page.evaluate('() => window.revealRadioSeeds()')

    expect(first).to_be_visible()
    expect(first).to_be_enabled()
    expect(first).to_be_focused()
    assert radio_ui.stops == ['POST']
    assert len(radio_ui.starts) == 1
    assert page.evaluate('sessionStorage.getItem("radio-launch")') is None


def test_resuming_session_waits_for_history_and_keeps_it_visible_during_refresh(page, radio_ui):
    api = radio_ui
    api.hold_playlist = True
    with page.expect_request("**/radio/playlist", timeout=6000):
        api.state = api.ready
    launch = page.get_by_role("status", name="Starting radio")
    expect(launch).to_be_visible(timeout=6000)
    expect(launch).to_contain_text("Returning to your radio")
    expect(launch.locator("button:visible, a:visible")).to_have_count(0)
    session = page.locator('.radio-session')
    expect(session).to_be_hidden()
    expect(page.locator('.radio-directions')).to_be_hidden()
    page.screenshot(path='/tmp/tuneconsole-radio-restoring.png')
    api.hold_playlist = False
    api.playlists.pop().fulfill(content_type='text/html', body=api.playlist)
    expect(session).to_be_visible(timeout=6000)
    expect(session.locator('table')).to_be_visible()
    expect(launch).to_be_hidden()

    # Later metadata refreshes retain the complete session while fetching its table.
    api.hold_playlist = True
    api.state = {**api.ready, 'metadata_revision': 1}
    with page.expect_request("**/radio/playlist", timeout=6000):
        page.evaluate("() => { Alpine.$data(document.querySelector('.radio-home')).poll(); }")
    expect(session).to_be_visible()
    expect(launch).to_be_hidden()
    api.hold_playlist = False
    api.playlists.pop().fulfill(content_type='text/html', body=api.playlist)
    expect(session).to_be_visible()












@pytest.mark.parametrize("failure", ["network"])
def test_polling_failure_preserves_session_and_recovers_quietly(page, radio_ui, failure):
    api = radio_ui
    api.state = api.ready
    expect(page.locator(".radio-session table")).to_be_visible(timeout=6000)

    def unavailable(route):
        if failure == "network":
            route.abort()
        elif failure == "http":
            route.fulfill(status=503)
        else:
            route.fulfill(content_type="application/json", body="temporarily unavailable")

    page.route("**/radio/status", unavailable)
    page.evaluate("() => Alpine.$data(document.querySelector('.radio-home')).poll()")
    expect(page.locator(".radio-session table")).to_be_visible()
    expect(page.locator(".radio-seeds")).to_be_hidden()
    expect(page.locator(".radio-home").get_by_role("alert")).to_be_hidden()

    page.unroute("**/radio/status", unavailable)
    api.state = {"active": False}
    expect(page.locator(".radio-seeds")).to_be_visible(timeout=6000)
    expect(page.locator(".radio-home").get_by_role("alert")).to_be_hidden()
