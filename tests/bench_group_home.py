"""Replay actual ranked pools and graph memberships in an isolated fixture.

uv run pytest -s tests/bench_group_home.py --group-readonly-db /path/to/state.db
The source is opened mode=ro/query_only, never through Store or migrations.
"""
import json
import logging
from contextlib import closing
from pathlib import Path
import sqlite3
import time

import pytest

from tests.conftest import seed_catalog_track
from yt_playlist.core.store import Store
from yt_playlist.rec import group_serving, home_cards, home_prepared


def test_actual_library_group_home_preparation_and_serving(tmp_path, request):
    database = request.config.getoption('group_readonly_db')
    if not database:
        pytest.skip('Supply --group-readonly-db to replay prepared actual-library inputs')
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as source:
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        generation = source.execute('SELECT generation FROM rec_group_head WHERE id=1').fetchone()[0]
        rows = {
            'rec_group_generations': source.execute(
                'SELECT * FROM rec_group_generations WHERE generation=?', (generation,)).fetchall(),
            'rec_group_identities': source.execute(
                'SELECT * FROM rec_group_identities WHERE group_id IN '
                '(SELECT group_id FROM rec_group_definitions WHERE generation=?)', (generation,)).fetchall(),
            'rec_group_definitions': source.execute(
                'SELECT * FROM rec_group_definitions WHERE generation=? ORDER BY rowid', (generation,)).fetchall(),
            'rec_group_members': source.execute(
                'SELECT m.* FROM rec_group_members m JOIN rec_group_definitions d USING(version) '
                'WHERE d.generation=?', (generation,)).fetchall(),
            'rec_group_head': [(1, generation)],
        }
        raw = source.execute("SELECT payload FROM rec_proposals WHERE surface='mode_bundles'").fetchone()[0]
    # Provider catalog ownership is recreated only in the disposable fixture.
    # This measures assembly against actual ranks and memberships; it does not
    # validate the user's current exclusions or foreground latency under load.
    bundles = json.loads(raw)
    pools = bundles['all']
    tracks = {t['key']: t for lane, pool in pools.items() if lane != 'fresh' for t in pool}
    store = Store(tmp_path / 'group-home.db')
    store.init_schema()
    request.addfinalizer(store.conn.close)
    for track in tracks.values():
        seed_catalog_track(store, track['key'], track['title'], track['artist'],
                           video_id=track['video_id'], genre=track.get('genre'), album=track.get('album'),
                           mb_year=str(track.get('year') or ''))
    with store.conn:
        for table, values in rows.items():
            if values:
                qs = ','.join('?' for _ in values[0])
                store.conn.executemany(f'INSERT INTO {table} VALUES ({qs})', values)
    now = time.time()
    store.rec_proposals.put_proposals('mode_bundles', bundles, now)
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
    members = {}
    for version, key, *_fit in rows['rec_group_members']:
        members.setdefault(version, set()).add(key)
    for menu in prepared['menus']:
        for proto in menu['protos']:
            if proto['lane'] == 'fresh':
                continue  # New discoveries remain outside the owned-library graph.
            assert len(proto['tracks']) == 12
            assert {t['key'] for t in proto['tracks']} <= members[proto['group_version']]
    report = {'generation': generation, 'pool_recordings': len(tracks), 'hierarchy_nodes': len(preview['groups']),
              'group_seconds': group_seconds, 'home_seconds': home_seconds, 'serve_seconds': serve_seconds,
              'serve_read_seconds': read_seconds,
              'preparation_attempts': attempts,
              'menus': len(prepared['menus']), 'mix_ready_regions': {
                  lane: len(menu) for lane, menu in preview['menus'].items()},
              'served_groups': [p['note'] for p in group_offers],
              'prepared_bytes': len(json.dumps(prepared)),
              'preview_bytes': len(json.dumps(preview))}
    output = request.config.getoption('group_report')
    if output:
        Path(output).write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
