"""Benchmark published group and Home inputs on a private library snapshot.

./scripts/test-library.py run -- pytest tests/bench_group_home.py -s --no-cov
"""
import logging
from contextlib import closing
import json
import time

import pytest

from tests.library_image.fixtures import managed_database, report_file
from yt_playlist.core.store import Store
from yt_playlist.rec import group_serving, home_cards, home_prepared


def test_actual_library_group_home_preparation_and_serving():
    report = {}
    database = managed_database(report=report)
    if database is None:
        pytest.skip('Run with scripts/test-library.py to use a private actual-library snapshot')
    store = Store(database)
    with closing(store.conn):
        store.init_schema()
        report.update(measure_preparation_and_serving(store))
    report_file('group-home-budgets.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


def measure_preparation_and_serving(store):
    generation = store.taste_groups.head_generation()
    assert generation and store.recommendations.head('home'), 'Use a library with published Groups and Home inputs'
    now = time.time()
    started = time.monotonic()
    preview = group_serving.prepare(store, now)
    group_seconds = time.monotonic() - started
    assert preview and preview['schema'] == group_serving.SCHEMA
    started = time.monotonic()
    attempts = []
    prepared = None
    for attempt in range(3):
        began = time.monotonic()
        prepared = home_prepared.prepare(store, now + attempt + 1)
        attempts.append(time.monotonic() - began)
        if prepared:
            break
    home_seconds = time.monotonic() - started
    assert prepared and prepared['group_policy'] == 'adaptive', 'Actual ranked pools must prepare within the Home budget'
    assert home_prepared._valid(prepared)
    service = home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))
    started = time.monotonic()
    home_prepared.cards(store, prepared, now + 2, service, record=False)
    read_seconds = time.monotonic() - started
    started = time.monotonic()
    served = home_prepared.cards(store, prepared, now + 2, service)
    serve_seconds = time.monotonic() - started
    group_offers = [p for p in served.protos if p['lane'] != 'fresh']
    assert group_offers and all(p.get('group_version') for p in group_offers)
    members = {group['version']: set(group['members'])
               for group in store.taste_groups.definitions(generation, members=True)}
    for menu in prepared['menus']:
        for proto in menu['protos']:
            if proto['lane'] == 'fresh':
                continue  # New discoveries remain outside the owned-library graph.
            assert len(proto['tracks']) == 12
            assert {t['key'] for t in proto['tracks']} <= members[proto['group_version']]
    return {'generation': generation, 'hierarchy_nodes': len(preview['groups']),
            'group_seconds': group_seconds, 'home_seconds': home_seconds, 'serve_seconds': serve_seconds,
            'serve_read_seconds': read_seconds,
            'preparation_attempts': attempts,
            'menus': len(prepared['menus']), 'mix_ready_regions': {
                lane: len(menu) for lane, menu in preview['menus'].items()},
            'served_groups': [p['note'] for p in group_offers],
            'prepared_bytes': len(json.dumps(prepared)),
            'preview_bytes': len(json.dumps(preview))}
