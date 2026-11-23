from math import dist
from pathlib import Path

import pytest
from playwright.sync_api import expect

from yt_playlist.core import updatecheck
from yt_playlist.core.migrations.annotation_storage import RETIRED


@pytest.fixture(autouse=True)
def development_build(monkeypatch):
    monkeypatch.setattr(updatecheck, 'is_dev_install', lambda: True)


def assert_clear_labels(root, names):
    labels = root.locator('.sd-landmark:visible')
    expect(labels).to_have_count(len(names))
    assert set(labels.locator('text').all_text_contents()) == names
    assert root.locator('#sd-canvas').evaluate('''canvas => {
      const viewport = canvas.getBoundingClientRect();
      const labels = [...canvas.querySelectorAll('.sd-landmark rect')]
        .map(label => label.getBoundingClientRect()).filter(label => label.width);
      return labels.every((label, index) => label.left >= viewport.left && label.right <= viewport.right
        && label.top >= viewport.top && label.bottom <= viewport.bottom
        && labels.slice(index + 1).every(other => label.right <= other.left || label.left >= other.right
          || label.bottom <= other.top || label.top >= other.bottom));
    }''')


def test_schema_map_shows_the_full_schema_with_relationships_in_place(live_app, page, request):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.goto(live_app + '/playlists')
    tools = page.get_by_role('button', name='Tools')
    tools.click()
    page.get_by_role('link', name='Schema map').click()
    root = page.locator('#schema-map')
    expect(root).to_have_attribute('data-mode', 'overview')
    diagram = root.locator('#sd-data').evaluate('node => JSON.parse(node.textContent)')
    assert 'filters' not in diagram  # Browsers can load new assets before the server restarts.
    cards = root.locator('[data-entity]')
    expect(cards).to_have_count(len(diagram['entities']))
    assert set(cards.evaluate_all('nodes => nodes.map(node => node.dataset.entity)')) == {
        entity['id'] for entity in diagram['entities']
    }
    assert set(root.locator('[data-edge]').evaluate_all('nodes => nodes.map(node => node.dataset.edge)')) == {
        edge['id'] for edge in diagram['edges']
    }
    for name in ('artists', 'albums', 'recordings', 'track_sources', 'accounts', 'playlist_scopes'):
        expect(root.locator(f'[data-entity="{name}"]')).to_have_class('sd-card sd-primary')
        expect(root.locator('#sd-labels').get_by_text(name, exact=True)).to_be_visible()
    main_names = set(
        root.locator('.sd-card.sd-primary').evaluate_all('cards => cards.map(card => card.dataset.entity)'))
    assert_clear_labels(root, main_names)
    output = Path(request.config.getoption('output'))
    page.mouse.move(100, 100)
    page.screenshot(path=str(output / 'schema-map-overview.png'))

    placements = cards.evaluate_all('''cards => Object.fromEntries(cards.map(card => {
      const {e: x, f: y} = card.transform.baseVal.getItem(0).matrix;
      return [card.dataset.entity, [x, y]];
    }))''')
    assert placements['artists'][0] < placements['entry_artists'][0] < placements['track_sources'][0]
    for attribute, owner, other in [
        ('artist_provider_ids', 'artists', 'recordings'),
        ('album_provider_ids', 'albums', 'track_sources'),
        ('recording_embedding_files', 'recordings', 'accounts'),
    ]:
        position = placements[attribute]
        assert dist(position, placements[owner]) < dist(position, placements[other]), attribute
    routing = root.locator('#sd-canvas').evaluate('''svg => {
      const cards = [...svg.querySelectorAll('[data-entity]')].map(card => {
        const {e: x, f: y} = card.transform.baseVal.getItem(0).matrix;
        const {width, height} = card.querySelector('rect').getBBox();
        return {x, y, right: x + width, bottom: y + height};
      });
      for (const [i, card] of cards.entries()) {
        if (cards.slice(i + 1).some(other => card.x < other.right && card.right > other.x
            && card.y < other.bottom && card.bottom > other.y)) return 'overlapping cards';
      }
      let bends = 0;
      for (const path of svg.querySelectorAll('[data-edge]')) {
        let previous;
        const points = Array.from(path.points);
        for (let i = 1; i < points.length; i++) {
          const [a, b] = [points[i - 1], points[i]];
          const vertical = a.x === b.x;
          if (!vertical && a.y !== b.y) return 'diagonal';
          if (previous !== undefined && previous !== vertical) bends++;
          previous = vertical;
          const overlaps = (p, q, lo, hi) => Math.min(p, q) < hi && Math.max(p, q) > lo;
          if (cards.some(card => vertical
            ? card.x < a.x && a.x < card.right && overlaps(a.y, b.y, card.y, card.bottom)
            : card.y < a.y && a.y < card.bottom && overlaps(a.x, b.x, card.x, card.right))) return 'through card';
        }
      }
      return bends ? 'orthogonal with bends' : 'no bends';
    }''')
    assert routing == 'orthogonal with bends'
    query = root.get_by_role('searchbox', name='Find a table or view')
    entity_filter = root.get_by_role('combobox', name='Filter by primary entity')
    expect(entity_filter).to_have_value('')
    expect(entity_filter.locator('option').first).to_have_text('Full schema')
    options = set(entity_filter.locator('option').evaluate_all('options => options.map(option => option.value)'))
    assert options == {'', *main_names}
    assert {'playlists', 'taste_groups', 'measurements', 'predictions', 'radio_sessions'} <= main_names
    assert options.isdisjoint({'entry_artists', 'active_playlists', 'recording_embedding_files', 'catalog_tracks'})
    assert query.bounding_box()['y'] == entity_filter.bounding_box()['y']
    assert query.bounding_box()['height'] == entity_filter.bounding_box()['height']
    routes = root.locator('[data-edge]').evaluate_all('''paths => Object.fromEntries(
      paths.map(path => [path.dataset.edge, path.getAttribute('points')]))''')

    shown_cards = root.locator('.sd-card:visible')
    detail = root.get_by_role('complementary', name='Entity details')
    description = detail.locator('.sd-entity-description')
    filter_members = {}
    for owner in sorted(main_names):
        entity_filter.select_option(owner)
        shown = shown_cards.evaluate_all('''cards => Object.fromEntries(cards.map(card => {
          const {e: x, f: y} = card.transform.baseVal.getItem(0).matrix;
          return [card.dataset.entity, [x, y]];
        }))''')
        active = set(shown)
        filter_members[owner] = active
        assert owner in active
        neighbors = {endpoint for edge in diagram['edges'] if owner in (edge['source'], edge['target'])
                     for endpoint in (edge['source'], edge['target']) if endpoint in placements}
        assert neighbors <= active, (owner, neighbors - active)
        assert shown == {name: placements[name] for name in active}, owner
        assert root.locator('[data-edge]').evaluate_all('''paths => Object.fromEntries(
          paths.filter(path => getComputedStyle(path).display !== 'none')
            .map(path => [path.dataset.edge, path.getAttribute('points')]))''') == {
            edge['id']: routes[edge['id']] for edge in diagram['edges']
            if {edge['source'], edge['target']} <= active
        }, owner
        shown_cards.and_(root.locator(f'[data-entity="{owner}"]')).press('Enter')
        expect(detail.get_by_role('heading', name=owner, exact=True)).to_be_visible()
        expect(description).to_be_visible()
        assert description.inner_text().strip(), owner
    tables = {entity['id'] for entity in diagram['entities'] if entity['kind'] in ('table', 'virtual')}
    assert not (unassigned := sorted(tables - set().union(*filter_members.values()))), unassigned
    assert {
        'albums', 'album_provider_ids', 'album_artists', 'account_albums', 'album_tracklists',
        'album_track_occurrences', 'album_occurrence_sources', 'album_item_observations',
        'album_tracks', 'saved_album_memberships', 'album_description_requests',
    } <= filter_members['albums']
    assert {'albums', 'album_description_requests'} <= filter_members['album_description_requests']
    assert 'provider_usage' not in tables
    assert 'album_description_access' not in tables
    assert {'artist_provider_ids', 'artist_aliases', 'artist_credit_observations',
            'entry_artists'} <= filter_members['artists']
    assert {'artists', 'recordings', 'discovered_artists'} <= filter_members['discovery_origins']
    assert {'discovery_memberships', 'discovery_starter_tracks', 'discovery_artist_image_state'}.isdisjoint(tables)
    assert 'artist_listening_summary' in filter_members['play_events'] & filter_members['artists']
    assert {'measurements', 'measurement_samples', 'sample_attributes'} <= filter_members['measurements']
    assert {'measurements', 'predictions', 'recording_vibes', 'vibe_versions'} <= (
        filter_members['vibe_definitions'])
    assert 'vibe_model_file_writes' not in tables
    assert 'profile_file_writes' not in tables
    assert 'musical_source_observations' not in tables
    assert 'rec_musical_sources' in filter_members['track_sources']
    assert 'collection_file_writes' in filter_members['recordings'] & filter_members['track_sources']
    assert 'vibe_preparation_checkpoint' not in tables
    assert filter_members['group_versions'] == {
        'group_versions', 'group_members', 'group_generations', 'taste_groups',
        'settings', 'recommendation_policies',
    }
    assert {'taste_groups', 'group_generations', 'group_versions', 'group_members',
            'settings'} <= filter_members['taste_groups']
    assert 'group_evidence_references' not in tables
    assert 'group_preparation_state' not in tables
    playlist_state = {'cluster_canvas', 'rec_recipes'}
    preparation_inputs = {'unresolved_preparation_inputs', 'write_unresolved_preparation_inputs'}
    assert playlist_state.isdisjoint(tables)
    assert 'prepared_recording_inputs' in filter_members['recommendation_generations']
    assert preparation_inputs.isdisjoint(filter_members['recommendation_generations'])
    assert preparation_inputs.isdisjoint(filter_members['group_generations'])
    entity_filter.select_option('accounts')
    assert {'accounts', 'account_albums', 'listening_observations'} <= filter_members['accounts']
    query.fill('listening_observations')
    expect(root.locator('#sd-results button').first).to_contain_text('listening_observations')
    query.fill('')
    page.screenshot(path=str(output / 'schema-map-accounts.png'))

    for name in ('accounts', 'play_events', 'recordings', 'metadata_observations'):
        entity_filter.select_option(name)
        assert_clear_labels(root, filter_members[name])

    entity_filter.select_option('play_events')
    expect(shown_cards.and_(root.locator('[data-entity="listening_observations"]'))).to_be_visible()
    query.fill('recording_embedding_files')
    expect(root.locator('#sd-results')).to_have_text('No matching tables or views.')
    entity_filter.select_option('')
    expect(shown_cards).to_have_count(len(diagram['entities']))
    expect(root.locator('.sd-edge[hidden]')).to_have_count(0)
    expect(query).to_have_value('')

    query.fill('recording_embedding_files')
    expect(root.locator('#sd-results button').first).to_contain_text('recording_embedding_files')
    query.press('Enter')
    assert not errors, errors
    expect(root).to_have_attribute('data-mode', 'entity')
    expect(detail.get_by_role('heading', name='recording_embedding_files')).to_be_visible()
    expect(description).to_have_count(0)
    expect(detail.get_by_text('recording_id', exact=True)).to_be_visible()
    expect(detail.get_by_role('button', name='recordings', exact=True)).to_be_visible()
    expect(cards).to_have_count(len(diagram['entities']))
    assert cards.evaluate_all('''cards => Object.fromEntries(cards.map(card => {
      const {e: x, f: y} = card.transform.baseVal.getItem(0).matrix;
      return [card.dataset.entity, [x, y]];
    }))''') == placements
    assert set(root.locator('.sd-edge.sd-highlight').evaluate_all('nodes => nodes.map(node => node.dataset.edge)')) == {
        edge['id'] for edge in diagram['edges'] if 'recording_embedding_files' in (edge['source'], edge['target'])
    }
    page.mouse.move(100, 100)
    page.screenshot(path=str(output / 'schema-map-recording-embeddings.png'))
    detail.get_by_role('button', name='recordings', exact=True).click()
    expect(detail.get_by_role('heading', name='recordings', exact=True)).to_be_visible()
    page.screenshot(path=str(output / 'schema-map-recordings.png'))
    detail.get_by_role('button', name='Close details').click()
    expect(detail).to_be_hidden()
    root.get_by_role('button', name='Fit diagram').click()
    root.locator('#sd-labels .sd-landmark').filter(has_text='artists').click()
    expect(detail.get_by_role('heading', name='artists', exact=True)).to_be_visible()
    page.get_by_role('link', name='Playlists', exact=True).click()
    expect(root).to_have_count(0)
    tools.click()
    page.get_by_role('link', name='Schema map').click()
    expect(root).to_have_attribute('data-mode', 'overview')
    assert not errors, errors


def test_schema_map_shows_compact_audio_annotation_storage(live_app, page):
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.goto(live_app + '/schema-admin')
    root = page.locator('#schema-map')
    entity_filter = root.get_by_role('combobox', name='Filter by primary entity')
    visible_cards = root.locator('.sd-card:visible')

    entity_filter.select_option('audio_annotations')
    assert set(visible_cards.evaluate_all('cards => cards.map(card => card.dataset.entity)')) == {
        'audio_annotations', 'sample_attributes',
    }
    entity_filter.select_option('recordings')
    expect(visible_cards.and_(root.locator('[data-entity="recordings"]'))).to_be_visible()
    entity_filter.select_option('measurements')
    expect(visible_cards.and_(root.locator('[data-entity="audio_annotations"]'))).to_have_count(0)

    diagram = root.locator('#sd-data').evaluate('node => JSON.parse(node.textContent)')
    columns = {entity['id']: {column['name'] for column in entity['columns']} for entity in diagram['entities']}
    assert columns.keys().isdisjoint({*RETIRED, 'acousticbrainz_vibe_inputs', 'acousticbrainz_vibe_estimates',
                                     'audio_vibe_runs', 'vibe_measurements'})
    assert 'acousticbrainz_estimates' in columns['recordings']
    assert {'labels_json', 'archive_json', 'sample_1_id', 'sample_2_id', 'sample_3_id'} <= columns['audio_annotations']
    assert 'acousticbrainz_classifiers' in columns['rec_musical_links']
    assert 'used_by_model' in columns['measurements']
