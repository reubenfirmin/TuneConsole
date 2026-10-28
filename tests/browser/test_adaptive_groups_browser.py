import pytest
from playwright.sync_api import expect

from tests.group_support import publish, recordings
from tests.test_group_serving import hidden_ancestor_preview, pools
from yt_playlist.rec import group_preparation, group_serving


@pytest.mark.parametrize('width', [1440, 390])
def test_music_groups_are_prominent_searchable_and_used_by_home(live_app, store, page, width):
    from tests.test_group_home import library
    from yt_playlist.rec import home_prepared

    _tracks, head = library(store)
    assert home_prepared.prepare(store, 1200)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app + '/taste')
    expect(page.get_by_role('heading', name='Your music groups', exact=True)).to_be_visible()
    page.get_by_role('tab', name='Music groups', exact=True).click()
    section = page.locator('[aria-labelledby="taste-grouping-preview-title"]')
    expect(section.get_by_text('Home’s Mix tapes now draw from these groups.', exact=False)).to_be_visible()
    expect(section.get_by_role('heading', name='House', exact=True)).to_be_visible()
    search = section.get_by_role('searchbox', name='Find a group or artist')
    search.fill('jazz')
    expect(section.get_by_role('heading', name='Jazz', exact=True)).to_be_visible()
    expect(section.get_by_role('heading', name='House', exact=True)).to_be_hidden()
    # Background list replacement must preserve both the query and its effect.
    page.evaluate("htmx.ajax('GET', '/taste/grouping-preview', {target: '#taste-grouping-preview-body', swap: 'innerHTML'})")
    expect(search).to_have_value('jazz')
    expect(section.get_by_role('heading', name='House', exact=True)).to_be_hidden()
    search.fill('')
    expect(section.get_by_role('heading', name='House', exact=True)).to_be_visible()
    section.screenshot(path=f'/tmp/tuneconsole-139-live-groups-{width}.png')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.goto(live_app + '/home/cards')
    # The fragment's actual generated recipe contains graph attribution.
    assert head['generation'] in page.content()
    assert 'musical-group' in page.content()
    assert not errors


@pytest.mark.parametrize('width', [1440, 390])
def test_prepared_group_preview_explains_distinct_names_and_parentage(live_app, store, page, width):
    tracks = recordings([('House', 20, .1), ('House', 20, .95)])
    publish(store, tracks)
    pools(store, tracks)
    group_serving.prepare(store, 1100)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app + '/taste#groups')
    section = page.locator('[aria-labelledby="taste-grouping-preview-title"]')
    section.scroll_into_view_if_needed()
    expect(section.get_by_role('heading', name='Instrumental house')).to_be_visible()
    expect(section.get_by_role('heading', name='Vocal house')).to_be_visible()
    expect(section.get_by_text('Part of House', exact=True)).to_have_count(2)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors


@pytest.mark.parametrize('width', [1440, 390])
def test_hidden_group_names_keep_visible_ancestor_context(live_app, store, page, width, monkeypatch):
    preview = hidden_ancestor_preview(store)
    monkeypatch.setattr(store.taste_groups, 'head_generation', lambda: preview['generation'])
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app + '/taste#groups')
    section = page.locator('[aria-labelledby="taste-grouping-preview-title"]')
    section.scroll_into_view_if_needed()
    for version, parent in [('tech-house', 'House'), ('house-leaf', 'House'),
                            ('prog-rock', 'Classic Rock'), ('classic-rock', 'Classic Rock')]:
        card = section.locator(f'[data-group-version="{version}"]')
        expect(card.get_by_text('Part of ' + parent, exact=True)).to_be_visible()
    expect(section.locator('[data-group-version="house"]')).not_to_contain_text('Part of')
    expect(section.locator('[data-group-version="rock"]')).not_to_contain_text('Part of')
    assert section.locator('[data-group-version="hidden-house"]').count() == 0
    section.locator('#music-group-search').fill('Classic Rock')
    expect(section.locator('[data-group-version="prog-rock"]')).to_be_visible()
    expect(section.locator('[data-group-version="tech-house"]')).to_be_hidden()
    section.locator('#music-group-search').fill('')
    section.screenshot(path=f'/tmp/tuneconsole-139-visible-ancestors-{width}.png')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors


@pytest.mark.parametrize('width', [1440, 390])
def test_fresh_preview_updates_with_continuing_backlog_and_retains_last_good_on_failure(
        live_app, store, page, width):
    from dataclasses import replace
    from tests.conftest import seed_catalog_track
    from yt_playlist.rec import progressive

    for i in range(6):
        seed_catalog_track(store, f'preview {i}|artist {i}', f'Preview {i}', f'Artist {i}',
                           video_id=f'preview{i}', genre=('House', 'House', 'Jazz')[i % 3])
    with store.conn:
        store.conn.executemany('INSERT INTO rec_musical_sources VALUES (?,?,?,?,?,1)',
            [(f'source{i}', 'test', 'artist', f'artist{i}', '{}') for i in range(12)])
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app + '/taste#groups')
    section = page.locator('[aria-labelledby="taste-grouping-preview-title"]')
    section.scroll_into_view_if_needed()
    expect(section.get_by_text('Waiting for the first library recordings to be prepared.')).to_be_visible()
    expect(section.get_by_text('12 artist detail updates', exact=False)).to_be_visible()
    section.screenshot(path=f'/tmp/tuneconsole-139-startup-{width}.png')
    preparation = progressive.Preparation(store, budget=replace(progressive.Budget(), batch=3))
    assert preparation.capture(1000) and preparation.capture(1001)
    assert preparation.groups.step(1002) and group_serving.prepare(store, 1003)
    # The open page must refresh itself; there is no reload or synthetic HTMX event.
    expect(section.get_by_text('3 recordings grouped.', exact=True)).to_be_visible(timeout=15000)
    expect(section.get_by_text('11 artist detail updates', exact=False)).to_be_visible()
    expect(section.get_by_role('heading', name='House', exact=True)).to_be_visible()
    expect(section.get_by_role('heading', name='Jazz', exact=True)).to_be_visible()
    assert store.prepared_content.has_pending() and store.musical_profiles.pending()
    generation = section.locator('[data-generation]').get_attribute('data-generation')
    section.screenshot(path=f'/tmp/tuneconsole-139-prepared-{width}.png')
    group_preparation.report(store, 'failed', 1100, reason='preparation-failed')
    page.reload()
    section.scroll_into_view_if_needed()
    expect(section.get_by_text('Musical groups could not be updated. Preparation will retry automatically.')).to_be_visible()
    expect(section.get_by_text('Showing the last completed groups while the next update is prepared.')).to_be_visible()
    expect(section.locator('[data-generation]')).to_have_attribute('data-generation', generation)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors
