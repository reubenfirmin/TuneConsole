"""Radio artwork motion, restored shape and playback history geometry."""
from pathlib import Path

import pytest
from tests.template_support import web_templates
from playwright.sync_api import expect
from tests.browser.radio_ui_support import radio_ui


def _junction_snapshot(session):
    """Measure the rendered routes, independently of their animation mechanism."""
    return session.evaluate('''session => {
      const control = session.querySelector('.radio-tune').getBoundingClientRect();
      const cx = control.x + control.width / 2, cy = control.y + control.height / 2;
      const paths = ['line', 'flow'].map(name => {
        const path = session.querySelector('.radio-junction-' + name);
        const matrix = path.getScreenCTM(), length = path.getTotalLength();
        const hub = Alpine.$data(session.closest('.radio-home')).junctionHub;
        const clip = session.querySelector('#radio-junction-clearance path');
        const count = 256;
        const points = Array.from({length: count + 1}, (_, i) =>
          path.getPointAtLength(length * i / count));
        // The outline's bottom edge and the flow's turnaround are symmetric.
        // Sample those directly rather than depending on arc-length spacing.
        const commands = getComputedStyle(path).d.match(/[MLCZ]|-?\\d+(?:\\.\\d+)?(?:e[-+]?\\d+)?/gi);
        let at = 0, current, previous, maxJoinAngle = 0, maxStemSlope = 0;
        const feet = [];
        while (at < commands.length) {
          const command = commands[at++];
          const point = () => [+commands[at++], +commands[at++]];
          if (command === 'Z') break;
          if (command === 'M' || command === 'L') {
            current = point(); previous = null;
          } else if (command === 'C') {
            const a = point(), b = point(), end = point();
            const middleY = (current[1] + 3 * a[1] + 3 * b[1] + end[1]) / 8;
            if (middleY > hub.y + hub.ry + 4 && middleY < 111) {
              const dx = (a[0] - current[0] + 2 * (b[0] - a[0]) + end[0] - b[0]) * matrix.a;
              const dy = (a[1] - current[1] + 2 * (b[1] - a[1]) + end[1] - b[1]) * matrix.d;
              maxStemSlope = Math.max(maxStemSlope, Math.abs(dx / dy));
            }
            const tangent = [a[0] - current[0], a[1] - current[1]];
            // CSS serializes path coordinates to six significant digits. Tiny
            // endpoint handles outside the visible SVG cannot establish a kink.
            if (previous && current[1] > 0 && current[1] < 111.8 &&
                Math.min(Math.hypot(...previous), Math.hypot(...tangent)) > .5) {
              const cross = previous[0] * tangent[1] - previous[1] * tangent[0];
              const dot = previous[0] * tangent[0] + previous[1] * tangent[1];
              maxJoinAngle = Math.max(maxJoinAngle, Math.abs(Math.atan2(cross, dot)));
            }
            previous = [end[0] - b[0], end[1] - b[1]];
            current = end;
          }
          if (current?.[1] === 112) feet.push(current[0]);
        }
        const bottom = name === 'flow' ? points.filter(point => point.y >= 111.9).map(point => point.x) : feet;
        const stemX = (Math.min(...bottom) + Math.max(...bottom)) / 2;
        // Check the unmasked contour too: any arm crossing the hub's vertical
        // center must pass BELOW the control, during movement and when settled.
        const crossings = [];
        for (let i = 1; i < points.length; i++) {
          const a = points[i - 1], b = points[i];
          if ((a.x - hub.x) * (b.x - hub.x) < 0) {
            const y = a.y + (b.y - a.y) * (hub.x - a.x) / (b.x - a.x);
            if (y > 0 && y < 112) crossings.push(y);
          }
        }
        const visible = points.filter(point => clip.isPointInFill(point));
        const clearance = Math.min(...visible.map(point => {
          const screen = point.matrixTransform(matrix);
          return Math.hypot((screen.x - cx) / (control.width / 2 + 1),
            (screen.y - cy) / (control.height / 2 + 1));
        }));
        return {d: getComputedStyle(path).d,
          stemX: new DOMPoint(stemX, 112).matrixTransform(matrix).x, clearance, maxJoinAngle, maxStemSlope,
          underControl: crossings.every(y => y > (control.bottom + 1 - matrix.f) / matrix.d),
          crossings, bottomOfControl: (control.bottom + 1 - matrix.f) / matrix.d,
          clipped: points.length - visible.length};
      });
      return {control: {x: control.x, y: control.y, width: control.width, height: control.height}, paths};
    }''')


def _assert_accepted_junction_shape(session):
    """Compare both actual rendered contours with the frozen approved routing.

    A clearance mask or smooth tangents alone cannot pass this check: removing
    the broad shoulders or drawing through the hub changes the measured shape.
    """
    differences = session.evaluate('''session => {
      const model = Alpine.$data(session.closest('.radio-home'));
      return ['line', 'flow'].map((name, index) => {
        const path = session.querySelector('.radio-junction-' + name);
        const reference = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        reference.setAttribute('d', acceptedRadioJunction(model, model.artworkView.connectedSlot, !!index));
        const matrix = path.getScreenCTM();
        const samples = shape => {
          const length = shape.getTotalLength();
          const count = Math.ceil(length * Math.max(matrix.a, matrix.d) / 1.5);
          return Array.from({length: count + 1}, (_, i) => shape.getPointAtLength(length * i / count))
            .filter(point => point.y >= 0 && point.y <= 112)
            .map(point => point.matrixTransform(matrix));
        };
        const distance = (from, to) => {
          const grid = new Map(), key = (x, y) => x + ':' + y;
          for (const point of to) {
            const cell = key(Math.floor(point.x / 8), Math.floor(point.y / 8));
            if (!grid.has(cell)) grid.set(cell, []);
            grid.get(cell).push(point);
          }
          return Math.max(...from.map(point => {
            const x = Math.floor(point.x / 8), y = Math.floor(point.y / 8);
            let closest = Infinity;
            for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++)
              for (const other of grid.get(key(x + dx, y + dy)) || [])
                closest = Math.min(closest, Math.hypot(point.x - other.x, point.y - other.y));
            if (!Number.isFinite(closest))
              closest = Math.min(...to.map(other => Math.hypot(point.x - other.x, point.y - other.y)));
            return closest;
          }));
        };
        const actual = samples(path), accepted = samples(reference);
        return {name, difference: Math.max(distance(actual, accepted), distance(accepted, actual)),
          x: model.junctionX, fork: model.junctionForkX, hub: {...model.junctionHub}};
      });
    }''')
    for row in differences:
        assert row['difference'] is not None and row['difference'] < 4, row


def _assert_junction_motion(page, session, initial, target_x):
    frames = [initial]
    for _ in range(10):
        page.clock.run_for(48)
        frames.append(_junction_snapshot(session))
    page.clock.run_for(1500)
    final = _junction_snapshot(session)
    for frame in [*frames, final]:
        assert frame['control'] == initial['control'], 'The tuning control must stay fixed'
        for path in frame['paths']:
            assert path['clearance'] >= 1, f'Junction enters the tuning control: {path}'
            assert path['underControl'], f'The bend must pass below the control: {path["crossings"]}, bottom {path["bottomOfControl"]}'
            assert path['maxJoinAngle'] < .005, f'Junction has an angular curve join: {path["maxJoinAngle"]}'
            assert path['maxStemSlope'] < 3, f'The lower stem forms a horizontal shoulder: {path["maxStemSlope"]}'
    for index, name in enumerate(('outline', 'dotted route')):
        start = initial['paths'][index]['stemX']
        end = final['paths'][index]['stemX']
        assert abs(end - target_x) < 2, f'{name} must arrive at the previewed queue'
        assert abs(end - start) > 20, f'{name} must move visibly'
        positions = [frame['paths'][index]['stemX'] for frame in frames[1:]]
        low, high = sorted((start, end))
        intermediate = {round(x, 1) for x in positions if low + 2 < x < high - 2}
        assert len(intermediate) >= 3, f'{name} must move through multiple intermediate positions'
        assert len({frame['paths'][index]['d'] for frame in frames}) >= 4


@pytest.mark.parametrize('width', [1440, 390])
def test_restored_junction_preserves_all_four_stream_shapes_without_loader_art(page, radio_ui, width):
    api = radio_ui
    page.set_viewport_size({'width': width, 'height': 1000})
    page.add_script_tag(content=Path(__file__).with_name('radio_junction_baseline.js').read_text())
    api.playlist = web_templates().get_template('_partials/radio_playlist.html').render(
        session=api.ready, tracks=api.tracks)
    api.state = api.ready
    page.evaluate('void Alpine.$data(document.querySelector(".radio-home")).refresh()')
    session = page.locator('.radio-session')
    expect(session).to_be_visible(timeout=10000)
    page.clock.install()
    for slot in range(4):
        queue = session.locator(f'.radio-direction[data-slot="{slot}"]')
        queue.scroll_into_view_if_needed()
        queue.hover(force=True)
        page.clock.run_for(2000)
        _assert_accepted_junction_shape(session)
        for path in _junction_snapshot(session)['paths']:
            assert path['clearance'] >= 1
            assert path['underControl']
            assert path['maxJoinAngle'] < .005
        expect(page.locator('.radio-launch .gen-loader')).to_have_count(0)
        page.screenshot(path=f'/tmp/tuneconsole-411-final-{width}-stream-{slot + 1}.png')
    expect(session.locator('.radio-direction.is-active')).to_have_attribute('data-slot', '0')


def test_artwork_evolves_slowly_with_reduced_motion_and_pauses_when_hidden(page, radio_ui):
    api = radio_ui
    page.clock.install()
    page.emulate_media(reduced_motion='reduce')
    page.evaluate('''sessionStorage.setItem('radio-artwork', JSON.stringify({
      playlistId: 'PLRADIO', phase: 0, seed: 284,
    }))''')
    templates = web_templates()
    api.playlist = templates.get_template('_partials/radio_playlist.html').render(session=api.ready, tracks=api.tracks)
    api.state = api.ready
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    expect(page.locator('.radio-wave-landscape path')).to_have_count(14)
    page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).artworkRunning')
    flow = page.locator('.radio-junction-flow')
    expect(flow).to_have_css('animation-name', 'radio-stream-drift')
    expect(flow).to_have_css('animation-duration', '16s')
    expect(flow).to_have_css('animation-timing-function', 'linear')
    circulation = flow.evaluate('''el => {
      const length = el.getTotalLength();
      const samples = Array.from({length: 1000}, (_, i) => {
        const distance = (i + .5) / 1000 * length;
        const point = el.getPointAtLength(distance);
        return {x: point.x, y: point.y,
          dy: el.getPointAtLength(distance + .5).y - el.getPointAtLength(distance - .5).y};
      });
      const left = samples.find(p => p.x < 180 && p.y > 12 && p.y < 28);
      const right = samples.find(p => p.x > 820 && p.y > 12 && p.y < 28);
      const start = el.getPointAtLength(0), end = el.getPointAtLength(length);
      return {downLeft: left?.dy > 0, upRight: right?.dy < 0,
        closed: Math.hypot(start.x - end.x, start.y - end.y) < .01};
    }''')
    assert circulation == {'downLeft': True, 'upRight': True, 'closed': True}
    # The small dots have their own smooth clock, independent of the evolving
    # landscape. Four dash periods make the repeat visually seamless.
    offsets = flow.evaluate('''el => {
      const animation = el.getAnimations().find(a => a.animationName === 'radio-stream-drift');
      animation.pause();
      const offsets = [0, 1000, 15999, 16000].map(time => {
        animation.currentTime = time;
        return parseFloat(getComputedStyle(el).strokeDashoffset);
      });
      animation.play();
      return offsets;
    }''')
    assert -2 < offsets[1] - offsets[0] < -1.5
    assert abs(offsets[2] + 28) < .01
    assert abs(offsets[3] - offsets[0]) < .01
    expect(page.locator('#radio-landscape-0')).to_have_css('transition-duration', '0s')
    initial = page.locator('#radio-landscape-0').evaluate('el => getComputedStyle(el).d')
    phase = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    page.clock.run_for(20_000)
    elapsed = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') - phase
    assert .16 < elapsed < .19  # The fastest fold takes twelve minutes.
    expect(page.locator('#radio-landscape-0')).not_to_have_css('d', initial)
    expect(flow).not_to_have_css('stroke-dashoffset', '0px')

    bounds = page.evaluate('''() => {
      const model = Alpine.$data(document.querySelector('.radio-home'));
      const original = model.artworkPhase, layer = document.querySelector('.radio-wave-landscape');
      const probe = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      layer.append(probe);
      const boxes = [], shapes = new Map();
      for (const minute of [0, 1, 3, 6, 12, 30, 60, 180, 360, 720, 11.999, 12.001]) {
        model.artworkPhase = minute * Math.PI * 2 / 12;
        const samples = [];
        for (let line = 0; line < 14; line++) {
          probe.setAttribute('style', model.landscapeStyle(line) + '; transition: none !important');
          const box = probe.getBBox();
          boxes.push({x: box.x, y: box.y, width: box.width, height: box.height});
          const length = probe.getTotalLength();
          for (const fraction of [.2, .4, .6, .8]) {
            const point = probe.getPointAtLength(length * fraction);
            samples.push([point.x, point.y]);
          }
        }
        shapes.set(minute, samples);
      }
      const displacement = (a, b) => shapes.get(a).reduce((sum, point, i) =>
        sum + Math.hypot(point[0] - shapes.get(b)[i][0], point[1] - shapes.get(b)[i][1]), 0) / shapes.get(a).length;
      model.artworkPhase = original;
      probe.remove();
      // A session's seeded motion may return near its starting shape at one
      // particular instant. Require substantial evolution over the sampled
      // listening period rather than a specific old thirty-minute trajectory.
      const maximumChange = Math.max(...[1, 3, 6, 12, 30, 60, 180, 360, 720]
        .map(minute => displacement(0, minute)));
      return {boxes, maximumChange, boundaryStep: displacement(11.999, 12.001),
        width: model.drawingSpace.width,
        height: model.drawingSpace.historyHeight + model.drawingSpace.gapHeight};
    }''')
    assert bounds['maximumChange'] > bounds['width'] * .08
    assert bounds['boundaryStep'] < bounds['width'] * .001
    for box in bounds['boxes']:
        assert box['x'] < bounds['width'] and box['x'] + box['width'] > 0
        assert box['y'] < bounds['height'] and box['y'] + box['height'] > 0
        assert box['width'] > bounds['width'] * .9 and box['height'] > 20

    phase = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    for minute in (0, 30):
        page.evaluate('minute => { Alpine.$data(document.querySelector(".radio-home")).artworkPhase = minute * Math.PI * 2 / 12; }', minute)
        page.screenshot(path=f'/tmp/tuneconsole-radio-evolution-{minute}.png')
    page.evaluate('phase => { Alpine.$data(document.querySelector(".radio-home")).artworkPhase = phase; }', phase)

    page.get_by_role('tab').first.click()
    expect(page.locator('.radio-space-field')).to_be_hidden()
    expect(flow).to_have_css('animation-play-state', 'paused')
    phase = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    page.clock.run_for(30_000)
    assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') == phase
    page.get_by_role('tab', name='On air', exact=True).click()
    expect(page.locator('.radio-space-field')).to_be_visible()
    # A frame may run between the click and this check, but hidden time must
    # not be replayed or reset the drawing.
    resumed = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    assert phase <= resumed < phase + .01
    expect(flow).to_have_css('animation-play-state', 'running')
    page.clock.run_for(2000)
    assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') > phase
    page.emulate_media(reduced_motion='no-preference')
    page.wait_for_function('Alpine.$data(document.querySelector(".radio-home")).artworkRunning')
    phase = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    page.clock.run_for(500)
    assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') > phase

    page.evaluate('''() => {
      Object.defineProperty(document, 'hidden', {configurable: true, value: true});
      document.dispatchEvent(new Event('visibilitychange'));
    }''')
    expect(flow).to_have_css('animation-play-state', 'paused')
    phase = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    page.clock.fast_forward(30 * 60_000)
    assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') == phase
    # Isolate the short resumed-frame measurement from browser round trips.
    # Tab transitions above retain their normal asynchronous browser clock.
    page.clock.pause_at(page.evaluate('Date.now() / 1000') + .1)
    resumed = page.evaluate('''() => {
      delete document.hidden;
      document.dispatchEvent(new Event('visibilitychange'));
      return Alpine.$data(document.querySelector('.radio-home')).artworkPhase;
    }''')
    assert resumed == phase
    expect(flow).to_have_css('animation-play-state', 'running')
    page.clock.run_for(250)
    change = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') - phase
    assert 0 < change < .003
    # A stalled visible frame also drops elapsed time instead of racing through it.
    phase += change
    page.clock.fast_forward(30 * 60_000)
    change = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase') - phase
    assert 0 <= change < .003


def test_artwork_resumes_its_saved_shape_after_visiting_another_page(page, radio_ui):
    api = radio_ui
    templates = web_templates()
    api.playlist = templates.get_template('_partials/radio_playlist.html').render(session=api.ready, tracks=api.tracks)
    api.state = api.ready
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase = 15')
    page.get_by_role('link', name='Playlists', exact=True).click()
    saved = page.evaluate('JSON.parse(sessionStorage.getItem("radio-artwork"))')
    assert 15 <= saved['phase'] < 15.01
    page.get_by_role('link', name='Home', exact=True).click()
    page.get_by_role('tab', name='On air', exact=True).click()
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    resumed = page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkPhase')
    assert saved['phase'] <= resumed < saved['phase'] + .01
    assert page.evaluate('Alpine.$data(document.querySelector(".radio-home")).artworkSeed') == saved['seed']
    expect(page.locator('#radio-landscape-0')).to_have_css('transition-duration', '0s')


def test_record_cover_changes_immediately_when_replaying_history(page, radio_ui):
    api = radio_ui
    api.state = {**api.ready, 'position': 2, 'history_count': 3}
    templates = web_templates()
    api.playlist = templates.get_template('_partials/radio_playlist.html').render(
        session=api.state, tracks=api.tracks)
    expect(page.locator('.radio-session')).to_be_visible(timeout=6000)
    cover = page.locator('.radio-record-cover')
    expect(cover).to_have_attribute('href', api.tracks[2]['thumbnail'])
    assert page.evaluate('''async url => {
      const image = new Image(); image.src = url; await image.decode();
      return image.naturalWidth === 300;
    }''', api.tracks[2]['thumbnail'])
    page.clock.install()
    requests = []
    page.route('**/play', lambda route: requests.append(route))
    page.locator('.radio-history .pl-play').first.click()
    expect(cover).to_have_attribute('href', api.tracks[0]['thumbnail'])
    assert len(requests) == 1
    requests[0].fulfill(json={'ok': True})
    # The history stays unchanged when native playback returns to an earlier song too.
    page.evaluate('''video => window.dispatchEvent(new CustomEvent('bridge-status', {detail: {
      connected: true, now_playing: {video_id: video, playlist_ytm_id: 'PLRADIO'}
    }}))''', api.tracks[0]['video_id'])
    expect(cover).to_have_attribute('href', api.tracks[0]['thumbnail'])
    page.evaluate('''video => window.dispatchEvent(new CustomEvent('bridge-status', {detail: {
      connected: true, now_playing: {video_id: video, playlist_ytm_id: 'PLRADIO'}
    }}))''', api.tracks[1]['video_id'])
    expect(cover).to_have_attribute('href', api.tracks[1]['thumbnail'])
    expect(page.locator('.radio-history tr')).to_have_count(3)


@pytest.mark.parametrize('width,height', [(1440, 1000), (1792, 1398), (390, 1000)])
def test_session_history_rises_from_junction_and_queues_preview_without_switching(page, radio_ui, width, height):
    api = radio_ui
    page.set_viewport_size({'width': width, 'height': height})
    page.add_script_tag(content=Path(__file__).with_name('radio_junction_baseline.js').read_text())
    api.ready = {**api.ready, 'directions': [
        {**branch, 'tracks': api.tracks[branch['slot'] + 1:branch['slot'] + 7]}
        for branch in api.ready['directions']]}
    templates = web_templates()
    template = templates.get_template('_partials/radio_playlist.html')

    def playing(count):
        tracks = [{**track, 'album': 'Night album', 'album_browse': None,
                   'year': '2001', 'genre': 'ambient', 'duration': 230, 'liked': False}
                  for track in api.tracks]
        api.state = {**api.ready, 'tracks': tracks, 'history_count': count, 'position': count - 1,
                     'browser_age': 0, 'browser': {'player': {
                         'playlist_ytm_id': 'PLRADIO', 'video_id': tracks[count - 1]['video_id']}}}
        api.playlist = template.render(session=api.state, tracks=tracks)

    playing(1)
    session = page.locator('.radio-session')
    expect(session).to_be_visible(timeout=6000)
    rows = session.locator('.radio-history tr')
    expect(rows).to_have_count(1)
    expect(rows.last).to_have_attribute('aria-current', 'true')
    expect(session.get_by_role('columnheader', name='Year', exact=True)).to_have_count(1)
    expect(session.get_by_role('columnheader', name='Genre', exact=True)).to_have_count(1)
    expect(rows.last).to_contain_text('2001')
    expect(rows.last).to_contain_text('ambient')
    assert session.locator('.radio-empty-row').count() >= 2
    scroller = session.locator('.radio-track-scroll')
    assert abs(rows.last.bounding_box()['y'] + rows.last.bounding_box()['height'] - (height / 2 - 16)) < 3
    expect(scroller).to_have_css('border-bottom-width', '0px')
    expect(session.locator('.radio-direction-label')).to_have_count(0)
    expect(session.locator('.radio-queue-art img')).to_have_count(12)
    drawing = session.locator('.radio-history-art')
    expect(drawing).to_be_visible()
    field = page.locator('body > .radio-space-field')
    expect(field).to_be_visible()
    assert field.bounding_box() == {'x': 0, 'y': 0, 'width': width, 'height': height}
    expect(field).to_have_css('pointer-events', 'none')
    assert drawing.bounding_box()['height'] > 80
    junction_box = session.locator('.radio-junction-space').bounding_box()
    assert abs(drawing.bounding_box()['y'] + drawing.bounding_box()['height'] - (junction_box['y'] + junction_box['height'])) < 3
    expect(session.locator('.track-table')).to_have_css('opacity', '1')
    assert '.85' in rows.first.evaluate('el => getComputedStyle(el).backgroundColor')
    expect(drawing.locator('.radio-record-cover')).to_have_attribute('href', api.tracks[0]['thumbnail'])
    assert page.evaluate('''async url => {
      const image = new Image(); image.src = url; await image.decode();
      return image.naturalWidth === 300;
    }''', api.tracks[0]['thumbnail'])
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=f'/tmp/tuneconsole-radio-drawing-{width}.png')

    other = session.locator('.radio-direction').nth(1)
    # Scroll the phone's queue into view before measuring either endpoint.
    other.scroll_into_view_if_needed()
    page.mouse.move(0, 0)
    page.clock.install()
    page.clock.pause_at(page.evaluate('Date.now() / 1000') + .1)
    page.clock.run_for(2000)
    selections = []

    def record_selection(route):
        selections.append(route.request.post_data)
        route.fulfill(json={'ok': False})

    page.route('**/radio/select', record_selection)
    active = session.locator('.radio-direction.is-active')
    for preference in ('no-preference', 'reduce'):
        page.emulate_media(reduced_motion=preference)
        initial = _junction_snapshot(session)
        if preference == 'no-preference':
            other.hover(force=True)
        else:
            other.locator('.radio-direction-heading').focus()
        expect(other).to_have_class('radio-direction is-preview')
        box = other.bounding_box()
        _assert_junction_motion(page, session, initial, box['x'] + box['width'] / 2)
        expect(active).to_have_attribute('data-slot', '0')
        expect(active.locator('.radio-direction-heading')).to_have_attribute('aria-pressed', 'true')
        expect(other.locator('.radio-direction-heading')).to_have_attribute('aria-pressed', 'false')
        assert active.evaluate('el => getComputedStyle(el).borderColor') != other.evaluate('el => getComputedStyle(el).borderColor')
        assert selections == [], 'Hover and focus must not switch the playing stream'
        page.screenshot(path=f'/tmp/tuneconsole-radio-session-{width}-{preference}.png')
        preview = _junction_snapshot(session)
        page.mouse.move(0, 0)
        page.locator('.radio-menu > summary').focus()
        expect(other).not_to_have_class('radio-direction is-preview')
        box = active.bounding_box()
        _assert_junction_motion(page, session, preview, box['x'] + box['width'] / 2)
        expect(active).to_have_attribute('data-slot', '0')
        assert selections == []

    # The approved refinement is specifically the middle two streams, including
    # reversing the pull before its shoulders catch up with the foot.
    other.hover(force=True)
    page.clock.run_for(1500)
    _assert_accepted_junction_shape(session)
    page.screenshot(path=f'/tmp/tuneconsole-411-restored-{width}-02.png')
    third = session.locator('.radio-direction').nth(2)
    for direction, queue in [('right', third), ('left', other)]:
        initial = _junction_snapshot(session)
        queue.hover(force=True)
        page.clock.run_for(128)
        moving = _junction_snapshot(session)
        for path in moving['paths']:
            assert path['maxJoinAngle'] < .005
            assert path['clearance'] >= 1
        page.screenshot(path=f'/tmp/tuneconsole-411-{width}-{direction}-mid.png')
        box = queue.bounding_box()
        _assert_junction_motion(page, session, initial, box['x'] + box['width'] / 2)
        page.screenshot(path=f'/tmp/tuneconsole-411-restored-{width}-{direction}-settled.png')
        _assert_accepted_junction_shape(session)
    third.hover(force=True)
    page.clock.run_for(128)
    turning = _junction_snapshot(session)
    other.hover(force=True)
    immediate = _junction_snapshot(session)
    assert abs(immediate['paths'][0]['stemX'] - turning['paths'][0]['stemX']) < 1
    box = other.bounding_box()
    _assert_junction_motion(page, session, turning, box['x'] + box['width'] / 2)
    _assert_accepted_junction_shape(session)
    page.screenshot(path=f'/tmp/tuneconsole-411-{width}-reversed.png')
    assert selections == []
    page.clock.resume()

    playing(3)
    expect(rows).to_have_count(3, timeout=6000)
    expect(rows.last).to_have_attribute('aria-current', 'true')
    assert abs(rows.last.bounding_box()['y'] + rows.last.bounding_box()['height'] - (height / 2 - 16)) < 3
    expect(drawing.locator('.radio-record-cover')).to_have_attribute('href', api.tracks[2]['thumbnail'])
    playing(12)
    expect(rows).to_have_count(12, timeout=6000)
    expect(rows.last).to_have_attribute('aria-current', 'true')
    expect(session.locator('.radio-empty-row')).to_have_count(0)
    expect(drawing).to_be_visible()
    assert drawing.bounding_box()['height'] > 80
    assert scroller.evaluate('el => el.scrollTop') > 0
    assert abs(rows.last.bounding_box()['y'] + rows.last.bounding_box()['height'] - (height / 2 - 16)) < 3
    # Polling must leave the user free to scroll back through earlier tracks.
    scroller.evaluate('el => el.scrollTop = 0')
    with page.expect_response('**/radio/status', timeout=6000):
        pass
    page.evaluate('() => new Promise(requestAnimationFrame)')
    assert scroller.evaluate('el => el.scrollTop') == 0
