"""Opt-in clicks through the running app and the user's installed Music extension.

This changes playback and the saved radio queue. It never opens a database or
starts an application server. Normal runs skip it; use --run-live-radio explicitly.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect


def test_running_stream_song_controls(page, request, tmp_path):
    if not request.config.getoption('--run-live-radio'):
        pytest.skip('Requires explicit live Radio acceptance and a signed-in Music tab')
    origin = 'http://127.0.0.1:8765'
    output = Path(request.config.getoption('--live-radio-trace') or tmp_path / 'stream-play-live.json')
    trace = {'started_at': datetime.now(timezone.utc).isoformat(), 'events': [], 'passed': False}
    choice_times = {}

    def record(kind, **values):
        trace['events'].append({'at': datetime.now(timezone.utc).isoformat(), 'kind': kind, **values})

    def call(path, data=None):
        response = (page.request.get(origin + path, timeout=15000) if data is None else
                    page.request.post(origin + path, data=data, timeout=15000))
        assert response.ok, (path, response.status, response.text()[:500])
        return response.json()

    def snapshot():
        state, bridge = call('/radio/status'), call('/bridge/status')
        browser = state.get('browser') or {}
        record('observation', state={k: state.get(k) for k in (
            'active', 'can_play_songs', 'song_play_version', 'playlist_id', 'menu_id', 'revision', 'position',
            'updating', 'pending_slot', 'starting', 'error', 'observer_error', 'selection_error', 'browser_age')},
            saved_ids=[t['video_id'] for t in state.get('tracks', [])],
            directions=[{'id': b['id'], 'slot': b['slot'], 'active': b['active'],
                         'video_ids': [t['video_id'] for t in b['tracks']]} for b in state.get('directions', [])],
            browser={k: browser.get(k) for k in ('observerVersion', 'expected', 'player', 'native',
                                               'playback_synced', 'page_synced', 'pageVideoIds')},
            now_playing=bridge.get('now_playing'),
            sensor_revisions=[{k: v.get(k) for k in ('receiverRevision', 'mainRevision', 'mainVersion')}
                              for v in (bridge.get('sensor_health') or {}).get('versions', [])])
        return state, bridge

    def wait_for(check, description, timeout=75, interval=1000):
        deadline, announced = time.monotonic() + timeout, 0
        while time.monotonic() < deadline:
            current = snapshot()
            if check(*current):
                return current
            if time.monotonic() - announced >= 15:
                print('Waiting for ' + description, flush=True)
                announced = time.monotonic()
            page.wait_for_timeout(interval)
        pytest.fail('Timed out waiting for ' + description + '; trace: ' + str(output))

    def ready(state, bridge):
        browser = state.get('browser') or {}
        player = browser.get('player') or {}
        playing = bridge.get('now_playing') or {}
        return (state.get('active') and state.get('can_play_songs') is True and not state.get('updating')
                and not state.get('starting') and not state.get('error') and not state.get('observer_error')
                and state.get('browser_age') is not None and state['browser_age'] < 8
                and browser.get('playback_synced') and browser.get('page_synced')
                and player.get('playlist_ytm_id') == state.get('playlist_id')
                and player.get('video_id') == playing.get('video_id') and not playing.get('paused'))

    def sync_ui():
        page.evaluate("async () => { await Alpine.$data(document.querySelector('.radio-home')).refresh(); }")
        return page.evaluate("Alpine.$data(document.querySelector('.radio-home')).state")

    def choose(slot, index, inject_stale=False):
        state = sync_ui()
        branch = next(b for b in state['directions'] if b['slot'] == slot)
        index %= len(branch['tracks'])
        track = branch['tracks'][index]
        record('choice', slot=slot, index=index, proposal_id=branch['id'], menu_id=state['menu_id'],
               requested={k: track.get(k) for k in ('video_id', 'title', 'artist')},
               continuation=[t['video_id'] for t in branch['tracks'][index + 1:]])
        if inject_stale:
            # Reproduce a menu refresh racing the physical click. The actual
            # server must reject this first request, then accept the UI's retry.
            page.evaluate('''({slot, index}) => {
                const button = document.querySelectorAll('.radio-direction[data-slot="' + slot + '"] .radio-proposed-art')[index];
                button.addEventListener('click', () => {
                    Alpine.$data(document.querySelector('.radio-home')).state.menu_id = 'live-smoke-expired-menu';
                }, {capture: true, once: true});
            }''', {'slot': slot, 'index': index})
            record('inject_stale_menu', slot=slot, video_id=track['video_id'])
        choice_times[track['video_id']] = time.monotonic()
        with page.expect_response('**/radio/select', timeout=20000) as response:
            page.locator(f'.radio-direction[data-slot="{slot}"] .radio-proposed-art').nth(index).click()
            pending = page.locator(f'.radio-direction[data-slot="{slot}"] .radio-song-pending')
            expect(pending).to_be_visible(timeout=1000)
            expect(pending).to_contain_text(track['title'])
            spinner = pending.locator('.radio-song-spinner')
            expect(spinner).to_have_css('animation-play-state', 'running')
            skipped = page.locator(f'.radio-direction[data-slot="{slot}"] .is-skipping')
            expect(skipped).to_have_count(index)
            skipped_ids = skipped.evaluate_all('rows => rows.map(row => row.dataset.videoId)')
            assert skipped_ids == [song['video_id'] for song in branch['tracks'][:index]]
            record('pending_feedback', video_id=track['video_id'], title=pending.inner_text(),
                   elapsed_seconds=time.monotonic() - choice_times[track['video_id']],
                   animation=spinner.evaluate('el => ({name: getComputedStyle(el).animationName, state: el.getAnimations()[0]?.playState})'),
                   skipped_ids=skipped_ids)
            if index:
                expect(skipped.first).to_have_css('opacity', '0.35')
                expect(skipped.first).to_have_css('filter', 'grayscale(1)')
            expect(page.locator('.radio-selection-error:visible')).to_have_count(0)
            page.screenshot(path=str(output.with_name(output.stem + '-pending.png')), full_page=True)
        result = response.value.json()
        record('choice_response', result={k: result.get(k) for k in ('ok', 'reason', 'updating', 'pending_slot')})
        if inject_stale:
            assert not result.get('ok') and result.get('stale'), 'The controlled stale-menu reproduction did not occur'
        if not result.get('ok') and result.get('stale'):
            page.wait_for_function('''() => {
                const model = Alpine.$data(document.querySelector('.radio-home'));
                return model.pendingSong?.accepted || model.selectionMessage;
            }''', timeout=20000)
            accepted = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).pendingSong?.accepted === true')
            assert accepted, page.locator('.radio-selection-error:visible').all_text_contents()
            record('choice_recovered', video_id=track['video_id'], slot=slot)
            expect(skipped).to_have_count(index)
            expect(page.locator('.radio-selection-error:visible')).to_have_count(0)
        else:
            assert result.get('ok'), result.get('reason')
        return track['video_id'], [t['video_id'] for t in branch['tracks'][index + 1:]]

    def confirmed(video, slot, continuation=()):
        elapsed = None
        if video in choice_times:
            def audible(state, bridge):
                assert not state.get('selection_error'), state['selection_error']
                now = bridge.get('now_playing') or {}
                return (now.get('video_id') == video and now.get('playlist_ytm_id') == state.get('playlist_id')
                        and not now.get('paused') and not now.get('muted') and not now.get('tab_muted'))

            # The user accepts the measured 5.8s delay with immediate animated
            # feedback and a preview of the songs being skipped. Record timing;
            # the bounded wait detects a stalled switch, not a five-second SLO.
            remaining = max(0, 30 - (time.monotonic() - choice_times[video]))
            wait_for(audible, 'the selected song to start', timeout=remaining, interval=250)
            elapsed = time.monotonic() - choice_times[video]
            record('playback_started', video_id=video, elapsed_seconds=elapsed)
            expect(page.locator('.radio-song-pending:visible')).to_have_count(0, timeout=5000)
            expect(page.locator('.radio-proposed-track.is-skipping')).to_have_count(0)

        def playing(state, bridge):
            browser, now = state.get('browser') or {}, bridge.get('now_playing') or {}
            player = browser.get('player') or {}
            return (ready(state, bridge) and now.get('video_id') == video and not player.get('paused')
                    and (browser.get('native') or {}).get('videoIds') == [t['video_id'] for t in state['tracks']]
                    and len(next(b['tracks'] for b in state['directions'] if b['active'])) >= 6)
        state, bridge = wait_for(playing, 'exact playback and queue adoption of ' + video)
        branch = next(b for b in state['directions'] if b['active'])
        assert branch['slot'] == slot
        future = [t['video_id'] for t in branch['tracks']]
        assert future[:len(continuation)] == list(continuation), (future, continuation)
        assert video not in future, 'The playing song must leave the future stream'
        record('confirmed', video_id=video, slot=slot, future=future)
        print('Confirmed exact song, saved/native queue and refilled stream: ' + video, flush=True)
        return state, bridge

    try:
        initial, bridge = snapshot()
        assert initial.get('can_play_songs') is True, 'Restart TuneConsole before live song acceptance'
        assert initial.get('song_play_version', 0) >= 4, 'Restart TuneConsole to load the playback latency fix'
        paused = bridge.get('now_playing') or bridge.get('resumable_player') or {}
        if paused.get('paused'):
            assert call('/now-playing/toggle?action=play', {})['ok']
            record('resume_requested', video_id=paused.get('video_id'))
        wait_for(ready, 'the restored Radio Session', timeout=120)
        page.set_viewport_size({'width': 1440, 'height': 1100})
        page.goto(origin + '/#radio', timeout=30000)
        expect(page.locator('.radio-session')).to_be_visible(timeout=30000)
        page.on('request', lambda outgoing: record('selection_request', payload=outgoing.post_data_json)
                if urlsplit(outgoing.url).path == '/radio/select' else None)

        smoke = request.config.getoption('--live-radio-song-smoke')
        cases = [('middle', 2, 2)] if smoke else [('first', 1, 0), ('middle', 2, 2), ('last', 3, -1)]
        for label, slot, index in cases:
            print('Checking ' + label + ' stream song', flush=True)
            video, continuation = choose(slot, index, inject_stale=smoke)
            confirmed(video, slot, continuation)

        if smoke:
            page.screenshot(path=str(output.with_suffix('.png')), full_page=True)
            trace['passed'] = True
            return

        print('Checking playback from a paused session', flush=True)
        assert call('/now-playing/toggle?action=pause', {})['ok']
        wait_for(lambda s, b: (b.get('now_playing') or {}).get('paused') is True, 'paused playback')
        video, continuation = choose(0, 2)
        confirmed(video, 0, continuation)

        print('Checking rapid choices', flush=True)
        state = sync_ui()
        latest = next(b for b in state['directions'] if b['slot'] == 2)['tracks'][2]['video_id']
        record('rapid_choices', first_slot=1, first_index=1, latest_slot=2, latest_index=2, latest_video=latest)
        with page.expect_response(lambda response: response.url.endswith('/radio/select')
                                  and response.request.post_data_json.get('video_id') == latest, timeout=25000) as response:
            page.evaluate('''() => {
                document.querySelectorAll('.radio-direction[data-slot="1"] .radio-proposed-art')[1].click();
                document.querySelectorAll('.radio-direction[data-slot="2"] .radio-proposed-art')[2].click();
            }''')
        assert response.value.json().get('ok'), response.value.json()
        confirmed(latest, 2)
        page.wait_for_timeout(3000)
        assert (call('/bridge/status').get('now_playing') or {}).get('video_id') == latest

        print('Checking a song choice during a queued direction change', flush=True)
        sync_ui()
        with page.expect_response('**/radio/select', timeout=20000) as response:
            page.locator('.radio-direction[data-slot="1"] .radio-direction-heading').click()
        queued = response.value.json()
        record('transition_queued', ok=queued.get('ok'), updating=queued.get('updating'))
        assert queued.get('ok') and queued.get('updating'), queued
        video, continuation = choose(3, 1)
        confirmed(video, 3, continuation)

        # Open actual metadata pages from the rendered links. Their handlers must
        # not submit another song selection or disturb the settled playing song.
        expect(page.locator('.radio-direction .radio-proposed-album')).to_have_count(0)
        links = page.locator('.radio-proposed-artist, .radio-proposed-song').evaluate_all(
            "els => els.map(el => ({kind: el.classList.contains('radio-proposed-artist') ? 'artist' : 'song', href: el.href}))")
        for kind in ('artist', 'song'):
            link = next((link for link in links if link['kind'] == kind), None)
            if link:
                destination = urlsplit(link['href'])
                assert destination.netloc == urlsplit(origin).netloc
                with page.expect_navigation(timeout=30000):
                    page.locator(f'a[href="{destination.path + "?" + destination.query}"]').first.click()
                assert urlsplit(page.url).path == destination.path
                assert (destination.path == '/artist' if kind == 'artist' else destination.path.startswith('/track/'))
                record('metadata_navigation', kind_checked=kind, url=page.url)
                page.goto(origin + '/#radio', timeout=30000)
                expect(page.locator('.radio-session')).to_be_visible(timeout=30000)
        assert (call('/bridge/status').get('now_playing') or {}).get('video_id') == video
        page.screenshot(path=str(output.with_suffix('.png')), full_page=True)
        trace['passed'] = True
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        if not trace['passed'] and not page.is_closed() and page.locator('.radio-session').count():
            try:
                page.screenshot(path=str(output.with_suffix('.png')), full_page=True, timeout=10000)
            except Exception as exc:
                record('screenshot_failed', error=str(exc))
        output.write_text(json.dumps(trace, indent=2) + '\n')
        print('Live stream Play trace: ' + str(output), flush=True)
