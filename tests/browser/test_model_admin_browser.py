"""Inspect the force canvas, model chains, popup navigation and wheel zoom."""
from pathlib import Path
from statistics import median

import pytest
from playwright.sync_api import expect


PROBE = Path(__file__).with_name('model_admin_probe.js').read_text()


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


@pytest.mark.parametrize('width', [1440, 1920])
def test_force_graph_popup_tracing_filters_and_wheel(live_app, page, width, request):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1050})
    page.goto(live_app + '/model-admin')
    root = page.locator('#model-map')
    expect(root.locator('canvas')).to_have_count(1)
    settled(page)
    bounds = page.locator('#mm-canvas').bounding_box()
    assert bounds['x'] == 216
    assert bounds['x'] + bounds['width'] == width
    assert bounds['y'] < 80 and bounds['y'] + bounds['height'] == 1050
    assert not inspect(page, 'viewport')['overflowX']
    assert not inspect(page, 'viewport')['overflowY']
    expect(root.locator('.mm-totals, .mm-inspector, .mm-column-heads')).to_have_count(0)
    assert inspect(page, 'zoom') >= 1
    initial_view = inspect(page, 'view')
    # Opening the map should show consumers together with the model branches
    # feeding them, without first fitting the entire graph to tiny labels.
    assert inspect(page, 'visibleConnections')

    # Every real dependency must read in the same direction, with room for
    # labels. Consumer features sit downstream of the models that feed them.
    layout = inspect(page, 'topology')
    assert min(layout['gaps']) > 80
    assert not layout['overlaps']
    assert median(layout['features']) > median(layout['modelColumns'])
    areas = inspect(page, 'featureAreas')
    assert [area['title'] for area in areas] == [
        'Home & listening', 'Discovery & exploration', 'Albums', 'Playlists',
        'Library & onboarding', 'Taste & history', 'Backend']
    assert all(not area['hidden'] and area['members'] for area in areas)
    assert all(node['contained'] for area in areas for node in area['members'])
    routing = inspect(page, 'featureRouting')
    assert routing['count'] == sum(len(area['members']) for area in areas)
    assert not routing['blocked']
    for above, below in zip(areas, areas[1:]):
        assert max(node['bottom'] for node in above['members']) < min(node['top'] for node in below['members'])
    assert median(s['parent'] for s in layout['satellites']) < median(median(s['others']) for s in layout['satellites'])
    destination = Path(request.config.getoption('output'))
    page.screenshot(path=str(destination / f'model-force-{width}.png'))
    root.locator('.mm-color-key summary').click()
    expect(root.locator('.mm-color-key [data-mm-group]')).to_have_count(7)
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
    for source, target in [('musicnn', 'mood-aggressive'), ('mood-aggressive', 'audio-research'),
                           ('audio-research', 'vibe-release'), ('musicnn', 'vibe-release'),
                           ('vibe-release', 'musical-profile'),
                           ('musical-profile', 'groups'), ('groups', 'group-choice'),
                           ('group-choice', 'mixtapes'), ('musical-profile', 'on-air')]:
        assert inspect(page, 'linkPaint', id=source, expected=target)['alpha'] >= .8
    research = canvas_node(page, 'audio-research')['x']
    for source in ('musicnn', 'audio-descriptors', 'mood-relaxed', 'mood-aggressive',
                   'mood-happy', 'mood-sad', 'mood-deam'):
        assert canvas_node(page, source)['x'] < research
    for vibe in ('relax', 'focus', 'cruise', 'energize', 'uplift', 'release'):
        assert research < canvas_node(page, 'vibe-' + vibe)['x']
    page.screenshot(path=str(destination / f'model-force-full-trace-{width}.png'))
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
    popup.locator('[data-inspect="contexts"]').click()
    expect(page.get_by_role('dialog', name='Separate taste contexts', exact=True)).to_be_visible()
    page.keyboard.press('Escape')
    expect(root.locator('#mm-dialog')).not_to_be_visible()
    expect(root.locator('#mm-canvas')).to_be_focused()

    click_node(page, 'track-svd')
    page.get_by_role('button', name='Focus this flow', exact=True).click()
    settled(page)
    filtered_areas = inspect(page, 'featureAreas')
    assert all(area['hidden'] == (not area['members']) for area in filtered_areas)
    before = visible_ids(page)
    assert 'contexts' in before and 'deepseek' not in before
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
    assert errors == []
