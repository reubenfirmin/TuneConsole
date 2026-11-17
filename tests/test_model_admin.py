"""Architecture documentation is complete, connected and safe to serve read-only."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from yt_playlist.controllers.model_admin import graph
from yt_playlist.web.app import create_app


ROOT = Path(__file__).resolve().parents[1]


def test_inventory_has_definitions_sources_and_closed_connections():
    inventory = graph()
    nodes = {node['id']: node for node in inventory['nodes']}
    periods = {period['id'] for period in inventory['periods']}
    assert set(inventory['feature_areas']) == {node['app_area'] for node in nodes.values() if node['area'] == 'feature'}
    assert len(nodes) == len(inventory['nodes'])
    assert {'durable', 'recent', 'session', 'now'} <= periods
    for node in nodes.values():
        for field in ('output', 'inputs', 'maintenance', 'temporal'):
            assert node[field].strip(), (node['id'], field)
        assert node['layers'] and set(node['layers']) <= periods
        assert node['sources'] and all((ROOT / path).is_file() for path in node['sources'])
        assert node['incoming'] or node['outgoing'], node['id']
    pairs = {(edge['source'], edge['target']) for edge in inventory['edges']}
    assert len(pairs) == len(inventory['edges'])
    for edge in inventory['edges']:
        assert edge['source'] in nodes and edge['target'] in nodes
        assert edge['detail']
        assert any(link['id'] == edge['source'] for link in nodes[edge['target']]['incoming'])
        assert any(link['id'] == edge['target'] for link in nodes[edge['source']]['outgoing'])


def test_inventory_covers_audio_external_and_non_recommendation_models():
    from yt_playlist.model_catalog.audio import MOOD_HEADS, SPECIALISTS
    from yt_playlist.model_catalog.services import ACOUSTIC_HEADS

    nodes = {node['id']: node for node in graph()['nodes']}
    # Distinct artifacts and externally fitted heads must not disappear inside
    # a single vague "audio" node when the production inventory changes.
    assert 'shared-vibe' not in nodes
    assert {'musicnn', 'vibe-intensity', 'joint-vibe', 'audio-descriptors',
            'deepseek', 'metadata', 'taxonomy', 'lastfm', 'provider-radio',
            'ytm-related-artists', 'ytm-artist-catalog',
            'daily-selection', 'video-motion'} <= nodes.keys()
    assert len(SPECIALISTS) == 6
    manifest = json.loads((ROOT / 'extension/audio_model/mood_models.json').read_text())
    assert {head['id'] for head in manifest} == {head[0] for head in MOOD_HEADS}
    assert all('vibe-' + entry[0] in nodes for entry in SPECIALISTS)
    assert all('ab-' + key in nodes for key, _ in ACOUSTIC_HEADS)
    for name in ('mixtapes', 'on-air', 'tv', 'stories', 'album-notes',
                 'enrichment', 'analytics', 'model-maintenance'):
        assert nodes[name]['area'] == 'feature'
    assert {node['id'] for node in nodes.values() if node['feature_type'] == 'backend'} == {
        'model-maintenance', 'enrichment'}
    assert all(node['feature_type'] == 'interactive' for node in nodes.values()
               if node['area'] == 'feature' and node['id'] not in {'model-maintenance', 'enrichment'})
    assert nodes['deepseek']['layers'] == ('structural',)
    assert nodes['mixtapes']['app_area'] == nodes['on-air']['app_area'] == nodes['tv']['app_area'] == 'Home & listening'
    assert nodes['albums']['app_area'] == nodes['album-notes']['app_area'] == 'Albums'
    assert nodes['completion']['app_area'] == nodes['journeys']['app_area'] == 'Playlists'
    assert nodes['model-maintenance']['app_area'] == nodes['enrichment']['app_area'] == 'Backend'
    assert 'not calibrated' in nodes['choice-response']['maintenance'] or 'not a trained' in nodes['choice-response']['maintenance']
    edges = {(edge['source'], edge['target']): edge for edge in graph()['edges']}
    assert nodes['audio-research']['area'] == 'model'
    assert ('musicnn', 'audio-research') in edges
    for source in ('audio-descriptors', *('mood-' + key for key, *_ in MOOD_HEADS)):
        assert {target for start, target in edges if start == source} == {'audio-research'}
    assert ('ab-mood_sad', 'metadata') in edges
    assert ('metadata', 'musical-profile') in edges
    for key, *_ in SPECIALISTS:
        assert {source for source, target in edges if target == 'vibe-' + key} == {'musicnn', 'audio-research'}
        assert ('vibe-' + key, 'musical-profile') in edges
    assert nodes['related-artists']['area'] == 'model'
    assert {source for source, target in edges if target == 'related-artists'} == {'artist-svd', 'artist-content', 'lastfm'}
    assert {target for source, target in edges if source == 'related-artists'} == {'artist-discovery', 'completion'}
    assert {target for source, target in edges if source == 'ytm-related-artists'} == {'road-trip', 'onboarding'}
    assert ('ytm-artist-catalog', 'onboarding') in edges
    assert edges['provider-radio', 'mixtapes']['condition'] == 'Optional radio discovery enabled'
    assert edges['ppr', 'tv']['condition'] == 'Inherited fallback menu only'
    assert edges['mode-choice', 'mixtapes']['condition'] == 'Fallback assembly'


def test_resource_renders_inventory_and_navigation_without_preparation(store):
    # An empty catalog and no connected providers still give the full map.
    with TestClient(create_app(store, lambda: {}, background_work=False),
                    base_url='http://127.0.0.1') as client:
        response = client.get('/model-admin')
    assert response.status_code == 200
    assert 'href="/model-admin"' in response.text
    assert 'model-admin.js' in response.text
    encoded = response.text.split('<script type="application/json" id="mm-data">', 1)[1].split('</script>', 1)[0]
    inventory = json.loads(encoded)
    assert len(inventory['nodes']) == len(graph()['nodes'])
    assert 'Maintenance & grooming' in response.text
    assert 'Time horizon' in response.text
