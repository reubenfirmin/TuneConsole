"""Inspect the force canvas, model chains, popup navigation and wheel zoom."""
from pathlib import Path
from statistics import median

import pytest
from playwright.sync_api import expect

from yt_playlist.core import updatecheck


PROBE = Path(__file__).with_name('model_admin_probe.js').read_text()


@pytest.fixture(autouse=True)
def development_build(monkeypatch):
    monkeypatch.setattr(updatecheck, '_raw_version', lambda: '1.0.0.dev1')


def inspect(page, action, **arguments):
    return page.evaluate(PROBE, {'action': action, **arguments})


def wait_for(page, action, **arguments):
    page.wait_for_function(PROBE, arg={'action': action, **arguments})


def settled(page):
    expect(page.locator('#mm-canvas')).to_have_attribute('data-settled', 'true')


def canvas_node(page, identity):
    return inspect(page, 'position', id=identity)


def click_node(page, identity):
    point = canvas_node(page, identity)
    page.mouse.move(point['x'], point['y'])
    # Canvas hit testing follows the pointer on the animation frame.
    inspect(page, 'frame')
    page.mouse.click(point['x'], point['y'])


def visible_ids(page):
    return inspect(page, 'ids')


@pytest.mark.parametrize('identity,title,description,sources', [
    ('groups', 'Musical group hierarchy', 'retained listening, or a tracklist on a Collection album',
     ('listening-evidence', 'curated-collections')),
    ('enrichment', 'Library enrichment & corrections', 'prefers the library input when both apply',
     ('listening-evidence', 'curated-collections', 'discovered-track-pool')),
])
def test_catalog_scope_is_visible_to_its_consumers(live_app, page, request, identity, title, description, sources):
    page.set_viewport_size({'width': 1440, 'height': 1050})
    page.goto(live_app + '/model-admin#' + identity)
    panel = page.get_by_role('dialog', name=title, exact=True)
    expect(panel.get_by_text(description, exact=False)).to_be_visible()
    inputs = panel.get_by_role('region', name='Inputs from')
    for source in sources:
        expect(inputs.locator(f'[data-inspect="{source}"]')).to_be_visible()
    inputs.scroll_into_view_if_needed()
    page.screenshot(path=str(Path(request.config.getoption('output')) / f'model-{identity}-scope.png'))


@pytest.mark.parametrize('width', [1440, 1920])
def test_on_air_ranker_explains_intensity_and_genre_search(live_app, page, width, request):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1050})
    page.goto(live_app + '/model-admin#on-air')
    on_air = page.get_by_role('dialog', name='On Air', exact=True)
    expect(on_air.get_by_text('Where to find it', exact=True)).to_be_visible()
    expect(on_air.get_by_text('Open Home and select On air.', exact=True)).to_be_visible()
    expect(on_air.get_by_text('Maintenance & grooming', exact=True)).to_have_count(0)
    destination = Path(request.config.getoption('output'))
    page.screenshot(path=str(destination / f'model-feature-on-air-{width}.png'))
    on_air.get_by_role('region', name='Inputs from').locator('[data-inspect="on-air-ranker"]').click()
    ranker = page.get_by_role('dialog', name='On Air ranker', exact=True)
    search = ranker.get_by_text('the Energy slider sets the share of upcoming picks', exact=False)
    expect(search).to_contain_text('search the strongest available intensity bands first')
    expect(search).to_contain_text('variety picks favor the closest softer band to the preceding song')
    search.scroll_into_view_if_needed()
    expect(search).to_be_in_viewport()
    page.screenshot(path=str(destination / f'model-force-on-air-ranker-{width}.png'))

    sequencing = ranker.locator('[data-inspect="playlist-sequencing"]')
    expect(sequencing).to_contain_text('Keep the chosen song and its skipped-prefix exclusions through queue recovery')
    sequencing.scroll_into_view_if_needed()
    expect(sequencing).to_be_in_viewport()
    page.screenshot(path=str(destination / f'model-force-on-air-sequencing-{width}.png'))

    ranker.get_by_role('region', name='Inputs from').locator('[data-inspect="vibe-intensity"]').click()
    intensity = page.get_by_role('dialog', name='On Air intensity', exact=True)
    evidence = intensity.get_by_text('Energize and Release specialist measurements from captured audio', exact=False)
    expect(evidence).to_be_visible()
    expect(evidence).to_contain_text('neutral midpoint')
    maintenance = intensity.get_by_text('On Air reads current scores on Apply', exact=False)
    expect(maintenance).to_contain_text('during background fill and during tuned automatic continuation')
    expect(maintenance).to_contain_text('Untuned queue replenishment uses search bands')
    page.screenshot(path=str(destination / f'model-on-air-intensity-{width}.png'))
    assert errors == []


@pytest.mark.parametrize('width', [1440, 1920])
def test_force_graph_popup_tracing_filters_and_wheel(live_app, page, width, request):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1050})
    page.goto(live_app + '/model-admin')
    root = page.locator('#model-map')
    expect(root.locator('canvas')).to_have_count(1)
    settled(page)
    wait_for(page, 'stableView')
    first = canvas_node(page, 'now')
    page.mouse.click(first['x'], first['y'])
    popup = page.get_by_role('dialog', name='NOW effective taste', exact=True)
    expect(popup).to_be_visible()
    for layer in ('session', 'recent', 'durable'):
        expect(popup.locator(f'[data-inspect="{layer}"]')).to_be_visible()
    page.keyboard.press('Escape')
    empty = inspect(page, 'background')
    page.mouse.click(empty['x'], empty['y'])
    bounds = page.locator('#mm-canvas').bounding_box()
    assert bounds['x'] == 216
    assert bounds['x'] + bounds['width'] == width
    assert bounds['y'] < 80 and bounds['y'] + bounds['height'] == 1050
    assert not inspect(page, 'viewport')['overflowX']
    assert not inspect(page, 'viewport')['overflowY']
    expect(root.locator('.mm-totals, .mm-inspector, .mm-column-heads')).to_have_count(0)
    layers = [canvas_node(page, layer) for layer in ('now', 'session', 'recent', 'durable')]
    assert all(layer['y'] == pytest.approx(layers[0]['y']) for layer in layers)
    assert all(layer['width'] >= 126 for layer in layers)
    assert layers[0]['x'] - layers[0]['width'] / 2 >= bounds['x']
    assert layers[-1]['x'] + layers[-1]['width'] / 2 <= width
    assert all(right['x'] - left['x'] > (left['width'] + right['width']) / 2
               for left, right in zip(layers, layers[1:]))
    initial_view = inspect(page, 'view')
    destination = Path(request.config.getoption('output'))
    page.mouse.move(30, 30)
    page.screenshot(path=str(destination / f'model-force-{width}.png'))
    # Opening the map should show consumers together with the model branches
    # feeding them, without first fitting the entire graph to tiny labels.
    assert inspect(page, 'visibleConnections')

    # The model branches outside the time layer row run left to right.
    layout = inspect(page, 'topology')
    assert min(layout['gaps']) > 80
    feedback_count = page.locator('#mm-data').evaluate(
        'node => JSON.parse(node.textContent).edges.filter(edge => edge.feedback).length')
    assert len(layout['feedbackGaps']) == feedback_count and min(layout['feedbackGaps']) > 80
    assert not layout['overlaps'], [(left, right, canvas_node(page, left), canvas_node(page, right))
                                    for left, right in layout['overlaps']]
    assert median(layout['features']) > median(layout['modelColumns'])
    areas = inspect(page, 'featureAreas')
    assert [area['title'] for area in areas] == [
        'Home & listening', 'Discovery & exploration', 'Albums', 'Playlists',
        'Library & onboarding', 'Taste & history', 'Backend']
    assert all(not area['hidden'] and area['members'] for area in areas)
    assert all(node['contained'] for area in areas for node in area['members'])
    routing = inspect(page, 'featureRouting')
    assert not any(routing.values()), routing
    for above, below in zip(areas, areas[1:]):
        assert max(node['bottom'] for node in above['members']) < min(node['top'] for node in below['members']), (
            above['members'], below['members'])
    for producer, feature in (('mix-tape-preparation', 'mixtapes'), ('on-air-ranker', 'on-air'),
                              ('revisit-preparation', 'revisit'),
                              ('prepared-track-selections', 'track-discovery'),
                              ('prepared-artist-selections', 'artist-discovery'),
                              ('road-trip-allocation', 'road-trip'), ('prepared-album-candidates', 'albums')):
        source, consumer = canvas_node(page, producer), canvas_node(page, feature)
        assert abs(source['y'] - consumer['y']) < 2 * consumer['height'], (producer, feature, source, consumer)
    revisit, preparer = canvas_node(page, 'revisit'), canvas_node(page, 'revisit-preparation')
    assert revisit['y'] < canvas_node(page, 'tv')['y']
    assert revisit['x'] - preparer['x'] < 2 * revisit['width'], (preparer, revisit)
    assert median(s['parent'] for s in layout['satellites']) < median(median(s['others']) for s in layout['satellites'])
    root.locator('.mm-color-key summary').click()
    expect(root.locator('.mm-color-key [data-mm-group]')).to_have_count(8)
    page.screenshot(path=str(destination / f'model-force-colors-{width}.png'))
    root.locator('.mm-color-key summary').click()

    feature = inspect(page, 'nodePaint', id='on-air')
    backend = inspect(page, 'nodePaint', id='model-maintenance')
    model = inspect(page, 'nodePaint', id='musicnn')
    assert feature['fill'] == backend['fill'] != model['fill']
    assert len({feature['outline'], backend['outline'], model['outline']}) == 3
    assert feature['fill'] != feature['outline'] and backend['fill'] != backend['outline']
    assert feature['caption'] == 'FEATURE' and backend['caption'] == 'BACKEND'
    root.get_by_role('button', name='Fit graph', exact=True).click()
    page.wait_for_timeout(400)
    point = canvas_node(page, 'musicnn')
    page.mouse.move(point['x'], point['y'])
    expect(root).to_have_attribute('data-selected', 'musicnn')
    # Tracing must stay visibly active through every hop to its consumers.
    for source, target in [('musicnn', 'vibe-release'), ('vibe-release', 'vibe-feedback'),
                           ('musicnn', 'vibe-feedback'),
                           ('vibe-release', 'musical-profile'), ('musical-profile', 'content'),
                           ('musical-profile', 'artist-content'),
                           ('musical-profile', 'groups'), ('groups', 'group-choice'),
                           ('group-choice', 'mix-tape-preparation'), ('mix-tape-preparation', 'mixtapes'),
                           ('musical-profile', 'vibe-intensity'),
                           ('vibe-intensity', 'on-air-ranker')]:
        assert inspect(page, 'linkPaint', id=source, expected=target)['alpha'] > .3
    musicnn = canvas_node(page, 'musicnn')['x']
    feedback = canvas_node(page, 'vibe-feedback')['x']
    assert canvas_node(page, 'audio-descriptors')['x'] < feedback
    for vibe in ('relax', 'focus', 'cruise', 'energize', 'uplift', 'release'):
        assert musicnn < canvas_node(page, 'vibe-' + vibe)['x'] < feedback
    page.screenshot(path=str(destination / f'model-force-full-trace-{width}.png'))
    point = canvas_node(page, 'ytm-related-artists')
    page.mouse.move(point['x'], point['y'])
    expect(root).to_have_attribute('data-selected', 'ytm-related-artists')
    assert inspect(page, 'linkPaint', id='ytm-related-artists', expected='related-artists')['alpha'] >= .8
    for feature in ('artist-discovery-ranker', 'completion', 'road-trip-ranker', 'onboarding'):
        assert inspect(page, 'linkPaint', id='related-artists', expected=feature)['alpha'] >= .8
    for source, target in [('artist-discovery-ranker', 'discovered-artists'),
                           ('discovered-artists', 'prepared-artist-selections'),
                           ('prepared-artist-selections', 'artist-discovery'),
                           ('road-trip-ranker', 'road-trip-allocation'),
                           ('road-trip-allocation', 'road-trip')]:
        assert inspect(page, 'linkPaint', id=source, expected=target)['alpha'] > .3
    page.screenshot(path=str(destination / f'model-force-artist-ensemble-{width}.png'))

    for ranker, consumers in (
        ('track-neighbor-ranker', ('similar-songs',)),
        ('cluster-expansion', ('clusters',)),
        ('playlist-sequencing', ('mix-tape-preparation', 'road-trip-allocation', 'clusters', 'on-air-ranker')),
    ):
        point = canvas_node(page, ranker)
        page.mouse.move(point['x'], point['y'])
        expect(root).to_have_attribute('data-selected', ranker)
        for consumer in consumers:
            assert inspect(page, 'linkPaint', id=ranker, expected=consumer)['alpha'] >= .8

    for identity, paths in (
        ('provider-radio', (
            ('provider-radio', 'discovery-intake', 'discovered-track-pool', 'content',
             'personalized-track-scoring', 'fresh-ranker', 'prepared-fresh-pool',
             'mix-tape-preparation', 'mixtapes'),
            ('prepared-fresh-pool', 'prepared-track-selections', 'track-discovery'),
            ('prepared-fresh-pool', 'prepared-radio-pools', 'on-air-ranker', 'on-air'),
        )),
        ('ranked-library-pools', (
            ('listening-evidence', 'ranked-library-pools', 'mix-tape-preparation', 'mixtapes'),
            ('ranked-library-pools', 'prepared-radio-pools', 'on-air-ranker', 'on-air'),
            ('ranked-library-pools', 'tv-video-preparation'),
            ('ranked-library-pools', 'prepared-vibe-fresh-pool'),
        )),
        ('revisit', (('listening-evidence', 'revisit-preparation', 'revisit'),)),
    ):
        point = canvas_node(page, identity)
        page.mouse.move(point['x'], point['y'])
        expect(root).to_have_attribute('data-selected', identity)
        for path in paths:
            for source, target in zip(path, path[1:]):
                assert inspect(page, 'linkPaint', id=source, expected=target)['alpha'] > .3

    point = canvas_node(page, 'choice-response')
    page.mouse.move(point['x'], point['y'])
    expect(root).to_have_attribute('data-selected', 'choice-response')
    assert inspect(page, 'linkPaint', id='choice-response', expected='choices')['alpha'] >= .8
    for source, target in [('choices', 'contexts'), ('choices', 'recent'), ('choices', 'durable'),
                           ('recent', 'now'), ('durable', 'now')]:
        assert inspect(page, 'linkPaint', id=source, expected=target)['alpha'] > .3
    page.screenshot(path=str(destination / f'model-force-choice-attribution-{width}.png'))

    point = canvas_node(page, 'recent')
    page.mouse.move(point['x'], point['y'])
    expect(root).to_have_attribute('data-selected', 'recent')
    for feature in ('now', 'prepared-artist-selections', 'stories'):
        assert inspect(page, 'linkPaint', id='recent', expected=feature)['alpha'] >= .8
    page.screenshot(path=str(destination / f'model-force-recent-{width}.png'))

    radio = canvas_node(page, 'on-air')
    page.mouse.move(radio['x'], radio['y'])
    expect(root).to_have_attribute('data-selected', 'on-air')
    for source, target in [('ranked-library-pools', 'prepared-radio-pools'),
                           ('prepared-fresh-pool', 'prepared-radio-pools'),
                           ('vibe-intensity', 'prepared-radio-pools'),
                           ('metadata', 'prepared-radio-pools'),
                           ('ppr', 'prepared-radio-pools'),
                           ('prepared-radio-pools', 'on-air-ranker'),
                           ('on-air-session-inputs', 'on-air-ranker'),
                           ('playlist-sequencing', 'on-air-ranker'),
                           ('on-air-ranker', 'on-air')]:
        assert inspect(page, 'linkPaint', id=source, expected=target)['alpha'] > .3
    page.screenshot(path=str(destination / f'model-force-on-air-{width}.png'))

    tv = canvas_node(page, 'tv')
    motion = canvas_node(page, 'video-motion')
    queue = canvas_node(page, 'tv-queue-selection')
    video_pool = canvas_node(page, 'published-video-pool')
    assert motion['x'] < queue['x'] < tv['x']
    assert motion['y'] + motion['height'] / 2 < min(video_pool['y'], queue['y'], tv['y'])
    assert queue['y'] - motion['y'] < 1.5 * queue['width']
    page.mouse.move(tv['x'], tv['y'])
    expect(root).to_have_attribute('data-selected', 'tv')
    for source, target in [('contexts', 'personalized-track-scoring'),
                           ('personalized-track-scoring', 'ranked-library-pools'),
                           ('personalized-track-scoring', 'fresh-ranker'),
                           ('fresh-ranker', 'prepared-fresh-pool'),
                           ('ranked-library-pools', 'tv-video-preparation'),
                           ('ranked-library-pools', 'tv-video-preparation'),
                           ('prepared-fresh-pool', 'tv-video-preparation'),
                           ('prepared-vibe-fresh-pool', 'tv-video-preparation'),
                           ('tv-video-preparation', 'published-video-pool'),
                           ('published-video-pool', 'tv-queue-selection'),
                           ('video-motion', 'tv-queue-selection'), ('tv-queue-selection', 'tv')]:
        assert inspect(page, 'linkPaint', id=source, expected=target)['alpha'] > .3
    page.screenshot(path=str(destination / f'model-force-tv-{width}.png'))

    point = canvas_node(page, 'road-trip')
    page.mouse.move(point['x'], point['y'])
    expect(root).to_have_attribute('data-selected', 'road-trip')
    for source, target in [('contexts', 'personalized-track-scoring'),
                           ('cold-projection', 'personalized-track-scoring'),
                           ('personalized-track-scoring', 'road-trip-ranker'),
                           ('road-trip-ranker', 'road-trip-allocation'),
                           ('road-trip-allocation', 'road-trip')]:
        assert inspect(page, 'linkPaint', id=source, expected=target)['alpha'] > .3
    page.screenshot(path=str(destination / f'model-force-road-trip-{width}.png'))

    point = canvas_node(page, 'choice-response')
    page.mouse.move(point['x'], point['y'])
    expect(root).to_have_attribute('data-selected', 'choice-response')
    for source in ('mixtapes', 'albums', 'on-air', 'listening-evidence'):
        assert inspect(page, 'linkPaint', id=source, expected='choice-response')['alpha'] >= .8
    page.screenshot(path=str(destination / f'model-force-choice-feedback-{width}.png'))
    click_node(page, 'choice-response')
    attribution = page.get_by_role('dialog', name='Choice outcome attribution', exact=True)
    expect(attribution).to_contain_text('zero preference weight')
    expect(attribution).to_contain_text('Missing outcomes remain unknown')
    assert set(attribution.locator('.mm-period').all_text_contents()) == {'NOW', 'TODAY', 'RECENT', 'PERMANENT'}
    for source in ('mixtapes', 'albums', 'on-air', 'listening-evidence'):
        expect(attribution.get_by_role('region', name='Inputs from').locator(f'[data-inspect="{source}"]')).to_be_visible()
    page.screenshot(path=str(destination / f'model-force-choice-details-{width}.png'))
    page.keyboard.press('Escape')
    for node, title in (('listening-evidence', 'Listening evidence'),
                        ('choices', 'Deliberate-choice interests')):
        click_node(page, node)
        details = page.get_by_role('dialog', name=title, exact=True)
        details.get_by_text('Implementation references', exact=True).click()
        source = details.get_by_text('src/yt_playlist/library/listening_window.py', exact=True)
        source.scroll_into_view_if_needed()
        expect(source).to_be_in_viewport()
        page.screenshot(path=str(destination / f'model-force-{node}-sources-{width}.png'))
        page.keyboard.press('Escape')
    root.get_by_role('button', name='Reset', exact=True).click()
    wait_for(page, 'reset', expected=initial_view)
    root.get_by_role('button', name='Fit graph', exact=True).click()
    page.wait_for_timeout(400)

    point = canvas_node(page, 'contexts')
    page.mouse.move(point['x'], point['y'])
    expect(root).to_have_attribute('data-selected', 'contexts')
    expect(root.locator('.mm-hover strong')).to_have_text('Separate taste contexts')
    expect(page.get_by_role('dialog')).to_have_count(0)
    page.screenshot(path=str(destination / f'model-force-hover-{width}.png'))

    # Wheel zoom belongs to the canvas and keeps the document stationary.
    old_zoom = inspect(page, 'zoom')
    old_areas = inspect(page, 'featureAreas')
    page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + bounds['height'] / 2)
    page.mouse.wheel(0, -450)
    wait_for(page, 'zoomed', zoom=old_zoom)
    inspect(page, 'frame')
    zoomed_areas = inspect(page, 'featureAreas')
    assert zoomed_areas[0]['width'] > old_areas[0]['width']
    assert all(node['contained'] for area in zoomed_areas for node in area['members'])
    assert inspect(page, 'viewport')['scrollY'] == 0
    page.wait_for_timeout(200)  # d3 completes a wheel gesture 150ms after its last event.
    origin = inspect(page, 'origin')
    background = inspect(page, 'background')
    page.mouse.move(background['x'], background['y'])
    inspect(page, 'frame')
    page.mouse.down()
    page.mouse.move(background['x'] - 80, background['y'] + 50, steps=8)
    page.mouse.up()
    shifted = inspect(page, 'origin')
    assert abs(shifted['x'] - origin['x']) > 50
    root.get_by_role('button', name='Fit graph', exact=True).click()
    page.wait_for_timeout(400)  # Finish the renderer's 350ms camera transition before hit testing.
    page.screenshot(path=str(destination / f'model-force-overview-{width}.png'))
    click_node(page, 'track-svd')
    popup = page.get_by_role('dialog', name='Track co-occurrence · SVD', exact=True)
    expect(popup).to_be_visible()
    expect(popup.locator('dt')).to_have_text([
        'Predicts or represents', 'Data & evidence', 'Maintenance & grooming', 'Time horizon'])
    page.screenshot(path=str(destination / f'model-force-popup-{width}.png'))
    popup.locator('[data-inspect="track-cooccurrence"]').click()
    popup = page.get_by_role('dialog', name='Track co-occurrence selector', exact=True)
    expect(popup.locator('[data-inspect="item2vec"]')).to_be_visible()
    popup.locator('[data-inspect="contexts"]').click()
    expect(page.get_by_role('dialog', name='Separate taste contexts', exact=True)).to_be_visible()
    page.keyboard.press('Escape')
    expect(root.locator('#mm-dialog')).not_to_be_visible()
    expect(root.locator('#mm-canvas')).to_be_focused()

    click_node(page, 'track-cooccurrence')
    page.get_by_role('button', name='Focus this flow', exact=True).click()
    settled(page)
    filtered_areas = inspect(page, 'featureAreas')
    assert all(area['hidden'] == (not area['members']) for area in filtered_areas)
    before = visible_ids(page)
    assert {'track-svd', 'item2vec', 'contexts'} <= set(before) and 'deepseek' not in before
    point = canvas_node(page, 'contexts')
    page.mouse.move(point['x'], point['y'])
    assert visible_ids(page) == before  # Hover does not rearrange the focused graph.
    page.screenshot(path=str(destination / f'model-force-focused-{width}.png'))

    root.get_by_role('button', name='Reset', exact=True).click()
    root.get_by_label('Find a model or feature').fill('DeepSeek')
    root.locator('#mm-results').get_by_role('button', name='DeepSeek album descriptions', exact=False).click()
    expect(root.locator('#mm-dialog')).to_contain_text('Outside the stack')
    root.locator('#mm-dialog [data-inspect="album-notes"]').click()
    expect(page.get_by_role('dialog', name='Album notes', exact=True)).to_be_visible()
    expect(page).to_have_url(live_app + '/model-admin#album-notes')
    page.get_by_role('button', name='Close model details', exact=True).click()
    root.get_by_role('button', name='Reset', exact=True).click()
    wait_for(page, 'reset', expected=initial_view)
    wait_for(page, 'stableView')
    camera = inspect(page, 'view')
    root.get_by_label('Time layer', exact=True).select_option('now')
    assert 'now' in visible_ids(page) and 'deepseek' not in visible_ids(page)
    settled(page)
    after = inspect(page, 'view')
    assert after['zoom'] == pytest.approx(camera['zoom'], abs=.001)
    assert after['center'] == pytest.approx(camera['center'], abs=.01)
    root.get_by_label('Find a model or feature').fill('no model has this name')
    expect(root.locator('.mm-empty')).to_be_visible()
    assert all(area['hidden'] for area in inspect(page, 'featureAreas'))
    root.get_by_role('button', name='Reset', exact=True).click()
    expect(root.locator('.mm-empty')).to_be_hidden()
    assert errors == []


@pytest.mark.parametrize('width', [1040, 1440, 1920])
def test_artist_ensemble_feeds_cold_projection(live_app, page, width, request):
    page.set_viewport_size({'width': width, 'height': 1050})
    page.goto(live_app + '/model-admin#artist-svd')
    root = page.locator('#model-map')
    details = page.get_by_role('dialog', name='Artist co-occurrence', exact=True)
    expect(details).to_be_visible()
    page.keyboard.press('Escape')
    settled(page)
    wait_for(page, 'stableView')
    chain = ('artist-svd', 'related-artists', 'cold-projection')
    taste_bottom = max(canvas_node(page, node)['y'] + canvas_node(page, node)['height'] / 2
                       for node in ('now', 'track-svd', 'content'))
    for identity in ('artist-svd', 'artist-content', 'related-artists'):
        node = canvas_node(page, identity)
        assert node['y'] - node['height'] / 2 > taste_bottom

    expect(root).to_have_attribute('data-selected', 'artist-svd')
    for source, target in zip(chain, chain[1:]):
        assert inspect(page, 'linkPaint', id=source, expected=target)['alpha'] >= .8
        assert canvas_node(page, source)['x'] < canvas_node(page, target)['x']
    root.evaluate('''(root, ids) => {
      const graph = root.modelGraph;
      const nodes = graph.graphData().nodes.filter(node => ids.includes(node.id));
      const left = Math.min(...nodes.map(node => node.x - node.width / 2));
      const right = Math.max(...nodes.map(node => node.x + node.width / 2));
      const top = Math.min(...nodes.map(node => node.y - node.height / 2));
      const bottom = Math.max(...nodes.map(node => node.y + node.height / 2));
      const zoom = Math.min(.9, (graph.width() - 96) / (right - left), (graph.height() - 220) / (bottom - top));
      graph.centerAt((left + right) / 2, (top + bottom) / 2, 0).zoom(zoom, 0);
    }''', [*chain, 'artist-content', 'now', 'track-svd', 'content'])
    wait_for(page, 'stableView')
    destination = Path(request.config.getoption('output'))
    page.screenshot(path=str(destination / f'model-force-artist-cooccurrence-{width}.png'))

    search = root.get_by_role('combobox', name='Find a model or feature')
    search.fill('Artist co-occurrence')
    search.press('Enter')
    details.locator('[data-inspect="related-artists"]').click()
    ensemble = page.get_by_role('dialog', name='Artist similarity ensemble', exact=True)
    ensemble.locator('[data-inspect="cold-projection"]').click()
    projection = page.get_by_role('dialog', name='Cold-track projection', exact=True)
    expect(projection.locator('[data-inspect="related-artists"]')).to_be_visible()
    expect(projection.locator('[data-inspect="artist-svd"]')).to_have_count(0)


def test_listening_evidence_shows_the_selected_media_requirement(live_app, page):
    page.goto(live_app + '/model-admin#listening-evidence')
    details = page.get_by_role('dialog', name='Listening evidence', exact=True)
    expect(details).to_contain_text('check the displayed playlist occurrence when available')
    expect(details).to_contain_text('verify playback progress and audio from the selected native media')
    expect(details).to_contain_text('a new native start of the same upload can earn credit after the same checks')
    details.get_by_text('Implementation references', exact=True).click()
    expect(details.get_by_text('src/yt_playlist/controllers/playback_commands.py', exact=True)).to_be_visible()
    expect(details.get_by_text('extension/content.js', exact=True)).to_be_visible()


def test_model_map_keeps_the_daily_holdout_and_omits_retired_trials(live_app, page):
    page.set_viewport_size({'width': 1440, 'height': 1050})
    page.goto(live_app + '/model-admin#daily-selection')
    settled(page)
    assert {'legacy-comparison', 'saved-trial-evaluation'}.isdisjoint(visible_ids(page))
    details = page.get_by_role('dialog', name='Daily embedding selection', exact=True)
    expect(details).to_contain_text('30-day')
    expect(details).to_contain_text('Date-only observations qualify when their entire local day fits within the window')
    expect(details).to_contain_text('Generated-only playback does not count')
    inputs = details.get_by_role('region', name='Inputs from')
    for source in ('listening-evidence', 'relationship-baskets', 'track-svd', 'item2vec'):
        expect(inputs.locator(f'[data-inspect="{source}"]')).to_be_visible()
    expect(inputs).to_contain_text('Use first-time plays in the 30-day holdout as evaluation targets.')
    output = details.get_by_role('region', name='Used by')
    expect(output.locator('[data-inspect="track-cooccurrence"]')).to_be_visible()
    expect(output).to_contain_text('New configuration promoted')


def test_model_maintenance_shows_direct_future_listening_inputs(live_app, page, request):
    page.set_viewport_size({'width': 1440, 'height': 1050})
    page.goto(live_app + '/model-admin#model-maintenance')
    settled(page)
    details = page.get_by_role('dialog', name='Automatic model maintenance', exact=True)
    inputs = details.get_by_role('region', name='Inputs from')
    listening = inputs.locator('[data-inspect="listening-evidence"]')
    expect(listening).to_contain_text('context and optional training weights, then scores later organic plays')
    baskets = inputs.locator('[data-inspect="relationship-baskets"]')
    expect(baskets).to_contain_text('playlist and listening baskets reconstructed before each cutoff')
    expect(inputs.locator('[data-inspect="daily-selection"]')).to_contain_text('comparison and promotion policy')
    for source in ('listening-evidence', 'relationship-baskets'):
        assert inspect(page, 'linkPaint', id=source, expected='model-maintenance')['alpha'] > 0.5
    inputs.scroll_into_view_if_needed()
    page.screenshot(path=Path(request.config.getoption('output')) / 'model-map-maintenance.png')


def test_co_membership_inspector_explains_account_day_baskets(live_app, page, request):
    page.set_viewport_size({'width': 1440, 'height': 1050})
    page.goto(live_app + '/model-admin#relationship-baskets')
    baskets = page.get_by_role('dialog', name='Co-membership evidence', exact=True)
    expect(baskets).to_contain_text('the same account and local calendar day')
    listening = baskets.get_by_role('region', name='Inputs from').locator('[data-inspect="listening-evidence"]')
    expect(listening).to_contain_text('Group eligible exact plays and date-only observations by account and local calendar day.')
    listening.scroll_into_view_if_needed()
    page.screenshot(path=Path(request.config.getoption('output')) / 'model-co-membership-evidence.png')

    for identity, title in (('track-svd', 'Track co-occurrence · SVD'),
                            ('item2vec', 'Track co-occurrence · Item2Vec')):
        baskets.get_by_role('region', name='Used by').locator(f'[data-inspect="{identity}"]').click()
        model = page.get_by_role('dialog', name=title, exact=True)
        expect(model).to_contain_text('eligible exact plays and date-only observations')
        expect(model).to_contain_text('local calendar day')
        model.get_by_role('region', name='Inputs from').locator('[data-inspect="relationship-baskets"]').click()


def test_story_inputs_explain_preparation_and_next_edition_feedback(live_app, page, request):
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.goto(live_app + '/model-admin#stories')
    details = page.get_by_role('dialog', name='Into recently & discovery stories', exact=True)
    inputs = details.get_by_role('region', name='Inputs from')
    for source in ('listening-evidence', 'recent', 'taxonomy', 'wikipedia-summaries', 'story-impressions'):
        expect(inputs.locator(f'[data-inspect="{source}"]')).to_be_visible()
    expect(inputs).to_contain_text('Home reads the prepared story groups.')
    expect(inputs).to_contain_text('Confirmed views give other subjects a turn in the next edition.')
    inputs.scroll_into_view_if_needed()
    page.screenshot(path=Path(request.config.getoption('output')) / 'model-story-inputs.png')

    inputs.locator('[data-inspect="story-impressions"]').click()
    impressions = page.get_by_role('dialog', name='Visible story impressions', exact=True)
    expect(impressions).to_contain_text('Fetching the Home fragment leaves the counts unchanged.')
    expect(impressions).to_contain_text('Refreshes within the six-hour edition keep using the same impression snapshot.')
    impressions.get_by_role('region', name='Used by').locator('[data-inspect="stories"]').click()
    inputs.locator('[data-inspect="wikipedia-summaries"]').click()
    wikipedia = page.get_by_role('dialog', name='Wikipedia summaries', exact=True)
    expect(wikipedia).to_contain_text("uses Wikipedia's article categories to resolve same-name artists")
    expect(wikipedia).to_contain_text('It skips ambiguous matches.')
    expect(wikipedia.get_by_role('region', name='Inputs from').locator('[data-inspect="metadata"]')).to_be_visible()


def test_revisit_preparation_explains_each_shelfs_listening_history(live_app, page, request):
    page.set_viewport_size({'width': 1440, 'height': 1050})
    page.goto(live_app + '/model-admin#revisit')
    feature = page.get_by_role('dialog', name='Revisit', exact=True)
    feature.get_by_role('region', name='Inputs from').locator('[data-inspect="revisit-preparation"]').click()
    preparation = page.get_by_role('dialog', name='Revisit preparation', exact=True)
    expect(preparation.get_by_text('the coldest playlists qualify without a fixed rest cutoff', exact=False)).to_contain_text(
        "the median of each distinct track's latest ordinary play. Unheard tracks count as cold")
    expect(preparation.get_by_text('Saved albums and familiar songs use dated listening', exact=False)).to_contain_text(
        'including generated playback. Quiet playlists use ordinary listening.')
    evidence = preparation.get_by_role('region', name='Inputs from').locator('[data-inspect="listening-evidence"]')
    expect(evidence).to_contain_text('Use ordinary and generated plays for album and song rest.')
    expect(evidence).to_contain_text("Rank playlists by their tracks' latest ordinary plays.")
    page.screenshot(path=Path(request.config.getoption('output')) / 'model-revisit-listening-evidence.png')


def test_feature_preparation_and_trace_depth(live_app, page, request):
    page.set_viewport_size({'width': 1440, 'height': 1050})
    page.goto(live_app + '/model-admin#provider-radio')
    root = page.locator('#model-map')
    root.get_by_role('button', name='Close model details').click()
    settled(page)
    wait_for(page, 'stableView')
    expect(root.locator('.mm-trace-key')).to_be_visible()
    legend = root.locator('.mm-legend').bounding_box()
    assert legend['x'] + legend['width'] < root.locator('.mm-zoom').bounding_box()['x']
    chain = ('provider-radio', 'discovery-intake', 'discovered-track-pool', 'content', 'personalized-track-scoring')
    paint = [inspect(page, 'linkPaint', id=source, expected=target) for source, target in zip(chain, chain[1:])]
    assert len({link['color'] for link in paint}) == 4
    for nearer, farther in zip(paint, paint[1:]):
        assert nearer['width'] > farther['width']
        assert nearer['alpha'] > farther['alpha']
    assert inspect(page, 'linkPaint', id='video-motion', expected='tv-queue-selection')['alpha'] < paint[-1]['alpha']

    routing = inspect(page, 'featureRouting')
    assert not any(routing.values()), routing
    for producer, downstream in (
        ('mix-tape-preparation', 'mixtapes'),
        ('prepared-radio-pools', 'on-air-ranker'), ('on-air-ranker', 'on-air'),
        ('published-video-pool', 'tv-queue-selection'), ('tv-queue-selection', 'tv'),
        ('audio-descriptors', 'vibe-feedback'), ('revisit-preparation', 'revisit'),
        ('prepared-track-selections', 'track-discovery'), ('artist-discovery-ranker', 'discovered-artists'),
        ('discovered-artists', 'prepared-artist-selections'),
        ('prepared-artist-selections', 'artist-discovery'), ('road-trip-ranker', 'road-trip-allocation'),
        ('road-trip-allocation', 'road-trip'), ('cluster-expansion', 'clusters'), ('cluster-connections', 'clusters'),
        ('track-neighbor-ranker', 'similar-songs'), ('album-ranker', 'prepared-album-candidates'),
        ('prepared-album-candidates', 'albums'), ('deepseek', 'album-notes'),
    ):
        source, target = canvas_node(page, producer), canvas_node(page, downstream)
        assert 0 < target['x'] - source['x'] < source['width'] + 1.6 * target['width'], (producer, downstream)
        assert abs(source['y'] - target['y']) < 2.5 * target['height'], (producer, downstream)

    destination = Path(request.config.getoption('output'))
    for identity in ('discovered-track-pool', 'vibe-mixes', 'clusters', 'road-trip', 'albums', 'taste-page'):
        root.evaluate('''(root, id) => {
          const graph = root.modelGraph;
          const node = graph.graphData().nodes.find(item => item.id === id);
          const x = node.x + (node.area === 'feature' ? -475 : 475);
          graph.centerAt(x, node.y, 0).zoom(.94, 0);
        }''', identity)
        wait_for(page, 'stableView')
        point = canvas_node(page, identity)
        page.mouse.move(point['x'], point['y'])
        expect(root).to_have_attribute('data-selected', identity)
        page.screenshot(path=str(destination / f'model-map-trace-{identity}.png'))


def test_new_track_discovery_traces_both_prepared_fresh_sources(live_app, page, request):
    page.set_viewport_size({'width': 1440, 'height': 1050})
    page.goto(live_app + '/model-admin#track-discovery')
    root = page.locator('#model-map')
    discovery = page.get_by_role('dialog', name='New-track discovery', exact=True)
    expect(discovery).to_be_visible()
    discovery.get_by_role('button', name='Focus this flow').click()
    root.get_by_role('button', name='Fit graph', exact=True).click()
    settled(page)
    wait_for(page, 'stableView')
    ranker, section = (canvas_node(page, identity) for identity in ('fresh-ranker', 'track-discovery'))
    zoom = inspect(page, 'zoom')
    page.mouse.move(ranker['x'] + .85 * (section['x'] - ranker['x']), (ranker['y'] + section['y']) / 2)
    page.mouse.wheel(0, -300)
    wait_for(page, 'zoomed', zoom=zoom)
    wait_for(page, 'stableView')
    page.mouse.move(30, 30)
    expect(root).to_have_attribute('data-selected', 'track-discovery')
    for pool in ('prepared-fresh-pool', 'prepared-vibe-fresh-pool'):
        assert inspect(page, 'linkPaint', id='fresh-ranker', expected=pool)['alpha'] > .3
        link = inspect(page, 'linkPaint', id=pool, expected='prepared-track-selections')
        assert link['alpha'] >= .8 and link['dash']
    destination = Path(request.config.getoption('output'))
    page.screenshot(path=str(destination / 'model-force-track-discovery-fresh-1440.png'))

    click_node(page, 'prepared-track-selections')
    detail = page.get_by_role('dialog', name='Prepared track selections', exact=True)
    expect(detail.locator('[data-inspect="prepared-fresh-pool"] em')).to_have_text('No Home Vibe active')
    expect(detail.locator('[data-inspect="prepared-vibe-fresh-pool"] em')).to_have_text('Home Vibe active')
    page.screenshot(path=str(destination / 'model-force-track-discovery-fresh-details-1440.png'))


def test_force_graph_survives_navigation_and_keyboard_inspection(live_app, page):
    def open_tool(name):
        tools = page.get_by_role('button', name='Tools', exact=True)
        if tools.get_attribute('aria-expanded') != 'true':
            tools.click()
        page.get_by_role('link', name=name, exact=True).click()
        expect(tools).to_have_attribute('aria-expanded', 'false')
        expect(page.locator('.tools-pop')).to_be_hidden()

    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.goto(live_app + '/network')
    inspect(page, 'rememberDocument')
    open_tool('Model map')
    expect(page.locator('#mm-canvas canvas')).to_have_count(1)
    settled(page)
    assert inspect(page, 'sameDocument')
    page.get_by_label('Find a model or feature').fill('MusiCNN')
    page.keyboard.press('ArrowDown')
    page.keyboard.press('Enter')
    expect(page.get_by_role('dialog', name='MusiCNN', exact=True)).to_be_visible()
    page.keyboard.press('Escape')
    open_tool('Network')
    expect(page.get_by_role('heading', name='Network log', exact=True)).to_be_visible()
    open_tool('Model map')
    expect(page.locator('#mm-canvas canvas')).to_have_count(1)
    page.get_by_label('Find a model or feature').fill('DeepSeek')
    page.keyboard.press('Enter')
    expect(page.get_by_role('dialog', name='DeepSeek album descriptions', exact=True)).to_be_visible()
    page.reload()
    expect(page.get_by_role('dialog', name='DeepSeek album descriptions', exact=True)).to_be_visible()
    page.keyboard.press('Escape')
    search = page.get_by_label('Find a model or feature')
    search.fill('Complete this playlist')
    search.press('Enter')
    completion = page.get_by_role('dialog', name='Complete a playlist', exact=True)
    expect(completion.get_by_text('Open a playlist and scroll below its tracks to Complete this playlist.', exact=True)).to_be_visible()
    page.keyboard.press('Escape')
    search.fill('Comfort listening')
    search.press('Enter')
    pools = page.get_by_role('dialog', name='Ranked library pools', exact=True)
    expect(pools).to_be_visible()
    for label in ('More in your wheelhouse', 'From your catalog', 'Comfort listening',
                  'Throwback, Time Flies and Recent Picks'):
        expect(pools.locator('.mm-definition')).to_contain_text(label)
    assert errors == []
