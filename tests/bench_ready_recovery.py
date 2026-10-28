"""Manual replay of the read-only export made by /tmp/tuneconsole-ready-export.py.

Run selected cases under the normal pytest lock. This opens only the disposable
snapshot, never the user's database. Policy history and old graph generations
are omitted; current catalog, history, exclusions, pools and evidence are real.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
import threading

from fastapi.testclient import TestClient
import pytest

from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence, home_prepared

SNAPSHOT = Path('/tmp/tuneconsole-ready-snapshot.db')


def snapshot_store():
    assert SNAPSHOT.is_file() and SNAPSHOT.parent == Path('/tmp')
    store = Store(SNAPSHOT)
    store.init_schema()
    return store


def test_actual_evidence_capacity_recovery():
    store = snapshot_store()
    ledger = store.rec_ledger.evidence()
    now = time.time()
    original = {row['id']: row for row in ledger.records('opportunity')}
    responses = ledger.records('response')
    protected = {row['opportunity_id'] for row in responses if row['kind'] not in {'open', 'visible'}}

    def graph_digest(identity, seen=None):
        from yt_playlist.core.evidence_ledger import _components, canonical
        seen = set() if seen is None else seen
        if identity in seen:
            return
        seen.add(identity)
        value = ledger.get('artifact', identity)
        assert value is not None, identity
        for child in _components(value):
            graph_digest(child, seen)
        return {key: hashlib.sha256(canonical(ledger.get('artifact', key))).hexdigest() for key in seen}

    protected_hashes = {key: graph_digest(original[key]['context']) for key in protected}
    report = {'initial_bytes': sum(p.stat().st_size for p in ledger.directory.glob('*/*/*.json')),
              'budget_bytes': ledger.byte_budget, 'protected_opportunities': len(protected), 'passes': []}
    report['initial_cleanup'] = choice_evidence.maintain_storage(store, now)
    # Real retained model-array shape and size, with a varying first coefficient.
    paths = [p for p in (ledger.directory / 'artifact').glob('*/*.json') if 2_000_000 < p.stat().st_size < 3_000_000]
    component = next(value for p in paths if isinstance(value := json.loads(p.read_bytes()), list))
    for index in range(96):
        at = now + index * 1800
        previous_paths = set(ledger.directory.glob('artifact/*/*.json'))
        value = deepcopy(component)
        value[0][0] += (index + 1) / 10000
        try:
            ref = ledger.artifact(value)
        except Exception:
            choice_evidence.maintain_storage(store, at)
            ref = ledger.artifact(value)
        oid = choice_evidence.issue(store, 'radio', [{'id': 'replay-a'}, {'id': 'replay-b'}], at,
            opportunity_id=f'ready393:{index}', context={'at': at, 'model': {'$artifact': ref}})
        if index % 24 == 0:
            choice_evidence.respond(store, oid, 'choice', at + 1, option_id='replay-a')
        # Maintenance does not depend on request activity. Age the simulated
        # publication's filesystem timestamp to its simulated wall clock.
        import os
        for path in set(ledger.directory.glob('artifact/*/*.json')) - previous_paths:
            os.utime(path, (at, at))
        result = ledger.maintain(at - 180 * 86400, orphan_before=at - 600,
                                pressure_before=at - 3600)
        report['passes'].append(result)
        assert result['used_bytes'] < result['budget_bytes']
        ledger = store.rec_ledger.evidence()  # reopen the independent ledger/counter
    for key in protected:
        assert ledger.get('opportunity', key) == original[key]
        assert graph_digest(original[key]['context']) == protected_hashes[key]
    assert sum(row['retired_for_capacity'] for row in report['passes']) > 0
    report['final_bytes'] = sum(p.stat().st_size for p in ledger.directory.glob('*/*/*.json'))
    assert int((ledger.directory / '.budget').read_text()) == report['final_bytes']
    Path('/tmp/tuneconsole-ready393-replay.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({key: value for key, value in report.items() if key != 'passes'}), flush=True)
    store.conn.close()


def test_actual_home_cold_preparation():
    import logging
    from yt_playlist.rec import home_cards
    from yt_playlist.web.app import create_app

    store = snapshot_store()
    with store.conn:
        store.conn.execute("DELETE FROM rec_proposals WHERE surface IN ('home_prepared','home_tuned')")
    attempts = []
    now = time.time()
    for index in range(5):
        start = time.monotonic()
        result = home_prepared.prepare(store, now + index * 31)
        attempts.append({'seconds': time.monotonic() - start,
                         'menus': len(result['menus']) if result else 0})
        print(attempts[-1], flush=True)
        if result:
            break
    Path('/tmp/tuneconsole-ready139-cold.json').write_text(json.dumps(attempts, indent=2))
    assert result and any(menu['protos'] for menu in result['menus'])
    # A minute rollover and ordinary sync publication cannot continually starve
    # the menu while optional history summaries are still being prepared.
    churn = []
    for index in range(3):
        at = now + 61 * (index + 1)
        store.settings.set_setting('last_sync_at', str(at))
        start = time.monotonic()
        updated = home_prepared.prepare(store, at)
        churn.append({'seconds': time.monotonic() - start, 'published': updated is not None})
        assert home_prepared.load(store, at)
    assert any(row['published'] for row in churn)
    service = home_cards.HomeCards(store, home_prepared._NoJobs(), logging.getLogger(__name__))
    served = home_prepared.cards(store, home_prepared.load(store, now), now, service, record=False)
    groups = [proto for proto in served.protos if proto.get('group_version')]
    assert groups and all(proto['recipe']['group_version'] == proto['group_version'] for proto in groups)
    with TestClient(create_app(store, lambda: {}), base_url='http://127.0.0.1') as client:
        response = client.get('/home/cards')
        assert response.status_code == 200 and 'musical-group' in response.text
    Path('/tmp/tuneconsole-ready139-churn.json').write_text(json.dumps({
        'cold_attempts': attempts, 'churn': churn,
        'groups': [proto['note'] for proto in groups]}, indent=2))
    store.conn.close()


@pytest.mark.parametrize('width', [1440, 390])
def test_actual_home_rendered_snapshot(page, width):
    import uvicorn
    from playwright.sync_api import expect
    from tests.browser_fixtures import _free_port
    from yt_playlist.web.app import create_app

    store = snapshot_store()
    assert home_prepared.load(store, time.time())
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(store, lambda: {}),
        host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started
        page.set_viewport_size({'width': width, 'height': 1000})
        page.goto(f'http://127.0.0.1:{port}/#directions')
        expect(page.get_by_role('tab', name='Mix tapes', exact=True)).to_have_attribute('aria-selected', 'true')
        page.wait_for_function('document.body.innerHTML.includes("musical-group")')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=f'/tmp/tuneconsole-139-actual-library-{width}.png')
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        store.conn.close()
