"""Architecture documentation is complete, connected and safe to serve read-only."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from yt_playlist.controllers.model_admin import graph
from yt_playlist.core import updatecheck
from yt_playlist.web.app import create_app


ROOT = Path(__file__).resolve().parents[1]


def test_inventory_has_definitions_sources_and_closed_connections():
    inventory = graph()
    nodes = {node['id']: node for node in inventory['nodes']}
    period_titles = {period['id']: period['title'] for period in inventory['periods']}
    periods = set(period_titles)
    assert set(inventory['feature_areas']) == {node['app_area'] for node in nodes.values() if node['area'] == 'feature'}
    assert len(nodes) == len(inventory['nodes'])
    assert period_titles == {'now': 'NOW', 'session': 'TODAY', 'recent': 'RECENT',
                             'durable': 'PERMANENT', 'structural': 'Outside the stack'}
    for node in nodes.values():
        assert node['summary'].strip(), node['id']
        if node['area'] == 'feature':
            assert node['ui_location'].strip(), node['id']
        else:
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


def test_taste_horizons_show_direct_evidence_and_consumers():
    inventory = graph()
    horizons = ('now', 'session', 'recent', 'durable')
    assert tuple(period['id'] for period in inventory['periods'][:4]) == horizons
    nodes = {node['id']: node for node in inventory['nodes']}
    for name in ('prepared-mix-listening', 'mix-tape-preparation', 'mixtapes'):
        assert nodes[name]['layers'] == ('now', 'session')
    edges = {(edge['source'], edge['target']) for edge in inventory['edges']}
    for layer in horizons:
        assert ('listening-evidence', layer) in edges
    assert {target for source, target in edges if source == 'choices'} == {
        'contexts', 'recent', 'durable', 'taste-page'}
    for layer in ('now', 'session'):
        assert ('content', layer) in edges
        assert ('groups', layer) in edges
    for layer in ('now', 'session', 'recent'):
        assert ('track-cooccurrence', layer) in edges
    assert ('listening-evidence', 'stories') in edges
    assert ('listening-evidence', 'mix-tape-preparation') in edges
    assert {target for source, target in edges if source == 'session'} == {'now', 'taste-page'}
    assert {target for source, target in edges if source == 'recent'} == {
        'now', 'stories', 'prepared-artist-selections', 'taste-page'}
    assert {target for source, target in edges if source == 'durable'} == {
        'now', 'taste-page', 'mix-tape-preparation', 'fresh-ranker'}
    assert {'album-ranker', 'on-air-ranker', 'personalized-track-scoring',
            'prepared-artist-selections', 'taste-page',
            'group-choice'} <= {target for source, target in edges if source == 'now'}
    assert {target for source, target in edges if source == 'prepared-mix-listening'} == {
        'mix-tape-preparation', 'personalized-track-scoring'}


def test_inventory_covers_audio_external_and_non_recommendation_models(monkeypatch):
    from yt_playlist.model_catalog.audio import SPECIALISTS
    from yt_playlist.model_catalog.services import ACOUSTIC_HEADS
    from yt_playlist.rec.audio_vibe_specialists import SPECIALISTS as RUNTIME_SPECIALISTS

    nodes = {node['id']: node for node in graph()['nodes']}
    # Distinct artifacts and externally fitted heads must not disappear inside
    # a single vague "audio" node when the production inventory changes.
    assert {'shared-vibe', 'ytm-artist-catalog'}.isdisjoint(nodes)
    assert {'musicnn', 'vibe-intensity', 'joint-vibe', 'acousticbrainz-vibes', 'audio-descriptors',
            'deepseek', 'metadata', 'taxonomy', 'lastfm', 'provider-radio',
            'ytm-related-artists',
            'daily-selection', 'video-motion'} <= nodes.keys()
    assert len(SPECIALISTS) == 6
    assert 'audio-research' not in nodes
    assert all(not key.startswith('mood-') for key in nodes)
    assert all('vibe-' + entry[0] in nodes for entry in SPECIALISTS)
    assert all('ab-' + key in nodes for key, _ in ACOUSTIC_HEADS)
    for name in ('mixtapes', 'on-air', 'tv', 'stories', 'album-notes', 'vibe-feedback',
                 'enrichment', 'analytics', 'model-maintenance'):
        assert nodes[name]['area'] == 'feature'
    assert {node['id'] for node in nodes.values() if node['feature_type'] == 'backend'} == {
        'model-maintenance', 'enrichment'}
    assert all(node['feature_type'] == 'interactive' for node in nodes.values()
               if node['area'] == 'feature' and node['id'] not in {'model-maintenance', 'enrichment'})
    assert nodes['deepseek']['layers'] == ('structural',)
    assert nodes['mixtapes']['app_area'] == nodes['on-air']['app_area'] == nodes['tv']['app_area'] == 'Home & listening'
    assert nodes['albums']['app_area'] == nodes['album-notes']['app_area'] == 'Albums'
    assert nodes['completion']['app_area'] == 'Playlists'
    assert nodes['playlist-sequencing']['area'] == 'model'
    assert nodes['model-maintenance']['app_area'] == nodes['enrichment']['app_area'] == 'Backend'
    assert 'heuristic' in nodes['choice-response']['maintenance']
    edges = {(edge['source'], edge['target']): edge for edge in graph()['edges']}
    assert {source for source, target in edges if target == 'choice-response'} == {
        'mixtapes', 'albums', 'on-air', 'listening-evidence'}
    assert {source for (source, target), edge in edges.items()
            if target == 'choice-response' and edge['feedback']} == {'mixtapes', 'albums', 'on-air'}
    assert ('choice-response', 'choices') in edges
    assert {'legacy-comparison', 'saved-trial-evaluation'}.isdisjoint(nodes)
    assert 'daily-selection' in nodes
    assert {source for source, target in edges if target == 'vibe-feedback'} == {
        'musicnn', 'audio-descriptors', *('vibe-' + key for key, *_ in SPECIALISTS)}
    for key, _ in ACOUSTIC_HEADS:
        head = 'ab-' + key
        assert ('acousticbrainz-match', head) in edges
        assert (head, 'musical-profile') in edges
        assert (head, 'acousticbrainz-vibes') in edges
        assert (head, 'joint-vibe') not in edges
    assert ('acousticbrainz-vibes', 'musical-profile') in edges
    assert ('acousticbrainz-vibes', 'vibe-intensity') not in edges
    assert all(('ab-' + key, 'metadata') not in edges for key, _ in ACOUSTIC_HEADS)
    assert ('metadata', 'musical-profile') in edges
    for target in ('content', 'artist-content'):
        assert ('musical-profile', target) in edges
        assert ('joint-vibe', target) not in edges
    for key, *_ in SPECIALISTS:
        assert {source for source, target in edges if target == 'vibe-' + key} == {'musicnn'}
        assert 'Artifact provenance' in nodes['vibe-' + key]['maintenance']
        assert ('vibe-' + key, 'musical-profile') in edges
        assert ('vibe-' + key, 'acousticbrainz-vibes') in edges
    for vibe, module in RUNTIME_SPECIALISTS:
        supported = []
        for mode in ('active', 'shadow', 'off'):
            monkeypatch.setenv(module.MODE_KEY, mode)
            if module.mode() == mode:
                supported.append(mode)
        key = 'focus' if vibe == 'concentrate' else vibe
        assert f"Supported modes are {'/'.join(supported)}." in nodes['vibe-' + key]['maintenance']
    assert nodes['related-artists']['area'] == 'model'
    assert {source for source, target in edges if target == 'related-artists'} == {
        'artist-svd', 'artist-content', 'lastfm', 'ytm-related-artists'}
    assert {target for source, target in edges if source == 'related-artists'} == {
        'artist-discovery-ranker', 'completion', 'road-trip-ranker', 'onboarding', 'cold-projection'}
    assert {source for source, target in edges if target == 'cold-projection'} == {
        'track-cooccurrence', 'metadata', 'taxonomy', 'musical-profile', 'related-artists'}
    assert {target for source, target in edges if source == 'ytm-related-artists'} == {'related-artists'}
    assert all('ytm-artist-catalog' not in edge for edge in edges)
    assert ('groups', 'ppr') in edges
    assert {'modes', 'mode-choice'}.isdisjoint(nodes)


def test_track_cooccurrence_serving_consumers_share_one_selector():
    edges = {(edge['source'], edge['target']): edge for edge in graph()['edges']}
    selector = 'track-cooccurrence'
    for method, condition in (('track-svd', 'SVD selected'), ('item2vec', 'Item2Vec selected')):
        assert {target for source, target in edges if source == method} == {selector, 'daily-selection'}
        assert edges[method, selector]['condition'] == condition
    assert {source for source, target in edges if target == selector} == {'track-svd', 'item2vec', 'daily-selection'}
    assert {target for source, target in edges if source == 'relationship-baskets'} == {
        'track-svd', 'item2vec', 'artist-svd', 'ppr', 'daily-selection', 'model-maintenance'}
    assert {target for source, target in edges if source == selector} == {
        'contexts', 'cold-projection', 'now', 'session', 'recent', 'track-neighbor-ranker',
        'cluster-expansion', 'cluster-connections', 'personalized-track-scoring', 'album-ranker', 'prepared-mix-listening',
        'artist-discovery-ranker',
    }


def test_seeded_exploration_map_traces_ranks_before_their_consumers():
    inventory = graph()
    nodes = {node['id']: node for node in inventory['nodes']}
    edges = {(edge['source'], edge['target']): edge for edge in inventory['edges']}
    assert 'journeys' not in nodes
    assert {source for source, target in edges if target == 'similar-songs'} == {'track-neighbor-ranker'}
    assert {source for source, target in edges if target == 'clusters'} == {
        'cluster-expansion', 'cluster-connections', 'taxonomy', 'playlist-sequencing',
        'metadata', 'discovered-track-pool'}
    for target in ('track-neighbor-ranker', 'cluster-expansion', 'cluster-connections'):
        assert ('track-cooccurrence', target) in edges
    assert ('metadata', 'track-neighbor-ranker') in edges
    assert ('content', 'track-neighbor-ranker') in edges
    assert ('content', 'cluster-expansion') in edges
    assert ('content', 'cluster-connections') not in edges
    assert {('playlist-sequencing', target) for target in
            ('mix-tape-preparation', 'road-trip-allocation', 'clusters')} <= edges.keys()
    assert ('playlist-sequencing', 'road-trip') not in edges
    assert edges['playlist-sequencing', 'clusters']['condition'] == 'When saving the mix'
    assert edges['playlist-sequencing', 'on-air-ranker']['condition'] == 'DJ style selected'
    assert {('road-trip-ranker', 'road-trip-allocation'), ('road-trip-allocation', 'road-trip')} <= edges.keys()
    assert ('road-trip-allocation', 'playlist-sequencing') not in edges
    assert {(source, 'road-trip-ranker') for source in
            ('related-artists', 'content', 'personalized-track-scoring')} <= edges.keys()
    assert ('now', 'road-trip-ranker') not in edges


def test_recommendation_consumers_trace_preparation_and_serving_boundaries():
    inventory = graph()
    nodes = {node['id']: node for node in inventory['nodes']}
    edges = {(edge['source'], edge['target']): edge for edge in inventory['edges']}
    for consumer, sources in {
        'mixtapes': {'mix-tape-preparation', 'starter-artists'},
        'tv': {'tv-queue-selection', 'video-motion'},
        'tv-queue-selection': {'published-video-pool', 'video-motion', 'metadata', 'tv'},
        'published-video-pool': {'tv-video-preparation'},
        'albums': {'prepared-album-candidates'},
        'artist-discovery': {'prepared-artist-selections', 'metadata'},
        'track-discovery': {'prepared-track-selections', 'taxonomy', 'now'},
        'revisit': {'revisit-preparation'},
        'on-air': {'on-air-ranker'},
    }.items():
        assert {source for source, target in edges if target == consumer} == sources

    assert not edges['mix-tape-preparation', 'mixtapes'].get('condition')
    assert 'published-menus' not in nodes
    assert edges['provider-radio', 'discovery-intake']['condition'] == 'radio_discovery_enabled (default off)'
    for consumer in ('prepared-track-selections', 'tv-video-preparation'):
        assert edges['prepared-fresh-pool', consumer]['condition'] == 'No Home Vibe active'
        assert edges['prepared-vibe-fresh-pool', consumer]['condition'] == 'Home Vibe active'
    assert {source for source, target in edges if target == 'prepared-vibe-fresh-pool'} == {
        'fresh-ranker', 'musical-profile', 'ranked-library-pools', 'taxonomy'}
    assert {target for source, target in edges if source == 'prepared-vibe-fresh-pool'} == {
        'prepared-track-selections', 'tv-video-preparation', 'vibe-mixes', 'mix-tape-preparation'}
    assert {source for source, target in edges if target == 'vibe-mixes'} == {
        'mix-tape-preparation', 'prepared-vibe-fresh-pool'}
    for path in (
        ('groups', 'group-choice', 'mix-tape-preparation', 'mixtapes'),
        ('provider-radio', 'discovery-intake', 'discovered-track-pool', 'content',
         'personalized-track-scoring', 'fresh-ranker', 'prepared-fresh-pool',
         'mix-tape-preparation'),
        ('cold-projection', 'personalized-track-scoring', 'fresh-ranker', 'prepared-fresh-pool'),
        ('personalized-track-scoring', 'road-trip-ranker', 'road-trip-allocation', 'road-trip'),
        ('prepared-fresh-pool', 'prepared-track-selections', 'track-discovery'),
        ('fresh-ranker', 'prepared-vibe-fresh-pool', 'prepared-track-selections', 'track-discovery'),
        ('related-artists', 'artist-discovery-ranker', 'discovered-artists', 'prepared-artist-selections', 'artist-discovery'),
        ('contexts', 'album-ranker', 'prepared-album-candidates', 'albums'),
        ('listening-evidence', 'revisit-preparation', 'revisit'),
        ('listening-evidence', 'ranked-library-pools', 'prepared-radio-pools', 'on-air-ranker', 'on-air'),
        ('ppr', 'prepared-radio-pools', 'on-air-ranker', 'on-air'),
        ('prepared-fresh-pool', 'on-air-ranker', 'on-air'),
        ('listening-evidence', 'on-air-session-inputs', 'on-air-ranker', 'on-air'),
    ):
        assert set(zip(path, path[1:])) <= edges.keys()
    assert edges['on-air', 'on-air-session-inputs']['feedback']
    assert ('musical-profile', 'vibe-intensity') in edges
    assert ('vibe-intensity', 'musical-profile') not in edges
    intensity = edges['vibe-intensity', 'on-air-ranker']['detail']
    assert 'current supported intensity scores' in intensity
    assert 'tuned automatic continuation' in intensity
    assert 'search bands' in edges['vibe-intensity', 'prepared-radio-pools']['detail']
    assert {target for source, target in edges if source == 'personalized-track-scoring'} == {
        'ranked-library-pools', 'fresh-ranker', 'road-trip-ranker'}
    assert 'negative' not in nodes
    assert not {'ranked-library-pools', 'ppr'} & {
        source for source, target in edges if target == 'on-air-ranker'}
    assert {'mix-tape-preparation', 'prepared-radio-pools', 'tv-video-preparation'} <= {
        target for source, target in edges if source == 'prepared-fresh-pool'}
    assert 'comfort-pool' not in nodes
    assert {target for source, target in edges if source == 'ranked-library-pools'} == {
        'mix-tape-preparation', 'prepared-radio-pools', 'tv-video-preparation', 'prepared-vibe-fresh-pool',
        'library-taste-summary'}
    assert ('library-taste-summary', 'mix-tape-preparation') in edges
    assert ('prepared-fresh-pool', 'library-taste-summary') in edges
    assert {target for source, target in edges if source == 'video-motion'} == {
        'tv-video-preparation', 'tv-queue-selection', 'tv'}
    assert edges['tv', 'tv-queue-selection']['feedback']
    assert edges['discovered-artists', 'discovered-albums']['feedback']
    assert nodes['revisit']['title'] == 'Revisit'
    assert nodes['artist-discovery']['app_area'] == nodes['track-discovery']['app_area'] == 'Discovery & exploration'


def test_resource_renders_inventory_and_navigation_without_preparation(store, monkeypatch):
    monkeypatch.setattr(updatecheck, '_raw_version', lambda: '1.0.0.dev1')
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


def test_release_hides_model_map_and_omits_its_route(store, monkeypatch):
    monkeypatch.setattr(updatecheck, '_raw_version', lambda: '1.0.0')
    monkeypatch.setattr(updatecheck, '_is_editable_install', lambda: False)
    with TestClient(create_app(store, lambda: {}, background_work=False),
                    base_url='http://127.0.0.1') as client:
        home = client.get('/')
        assert home.status_code == 200
        assert 'href="/model-admin"' not in home.text
        assert 'href="/taste"' in home.text
        assert client.get('/model-admin').status_code == 404
