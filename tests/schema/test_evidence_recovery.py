"""Foreground availability and eventual recovery on an isolated actual library."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time

from fastapi.testclient import TestClient
from playwright.sync_api import expect
import pytest
import uvicorn

from tests.browser_fixtures import _free_port
from tests.live_server import LiveServer
from yt_playlist.core import album_description_files, group_evidence, legacy_evidence
from yt_playlist.core.store import Store
from yt_playlist.rec import choice_context, choice_evidence, choice_retention, group_roots
from yt_playlist.web.app import create_app


def _copy_owned_files(source, target, source_path, target_path, report):
    """Copy immutable files exactly; change only their explicit SQL paths.

    This startup check does not rehearse the backup exporter's repacking and
    durable publication of every historical record. Each copied whole file is
    checked against its content-addressed name, including packed byte ranges.
    """
    domains = {
        'annotations': [('audio_annotations', 'model_output_filename')],
        'policies': [(name, 'configuration_filename') for name in (
            'recommendation_policy_bodies', 'recommendation_policies')],
        'descriptions': [(name, fields[1]) for name, fields in album_description_files.OWNERS.items()],
        'groups': [('group_versions', 'definition_filename'), ('group_generations', 'metadata_filename'),
                   ('group_lineage', 'evidence_filename')],
        'recommendations': [('prepared_outputs', 'filename')],
        'metadata': [('metadata_deliveries', 'raw_filename')],
        'archives': [(name, 'filename') for name in legacy_evidence.DOMAINS.values()],
    }
    copied = set()
    tables = {row[0] for row in source.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    for domain, owners in domains.items():
        old_root = source_path.with_name(source_path.name + '.' + domain)
        new_root = target_path.with_name(target_path.name + '.' + domain)
        for table, column in owners:
            if table not in tables:
                continue
            for filename, in source.execute(f'SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL'):
                path = Path(filename)
                relative = path.relative_to(old_root)
                if path in copied:
                    continue
                destination = new_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
                with destination.open('rb') as stream:
                    assert hashlib.file_digest(stream, 'sha256').hexdigest() == path.stem
                copied.add(path)
            target.execute(f'UPDATE {table} SET {column}=? || substr({column},?) WHERE {column} IS NOT NULL',
                           (str(new_root), len(str(old_root)) + 1))
        report['copied_domains'].append(domain)
    report['owned_files'] = len(copied)


@pytest.mark.browser
def test_actual_library_recovers_while_cached_home_remains_usable(request, tmp_path, monkeypatch, page):
    option = request.config.getoption('--schema-source-db')
    resume = request.config.getoption('--schema-recovery-db')
    if not option and not resume:
        pytest.skip('Supply --schema-source-db; the source is opened with SQLite mode=ro')
    # A retained isolated donor is usable only as a complete cohort: SQL and
    # every file domain come from that same donor, including listening evidence.
    source_path = Path(resume or request.config.getoption('--schema-upgraded-db') or option).resolve(strict=True)
    if resume:
        assert source_path.is_relative_to(Path(request.config.rootpath).resolve() / '.pytest_cache'), (
            'In-place recovery is limited to this checkout\'s retained test libraries')
    target_path = source_path if resume else tmp_path / 'isolated.db'
    report_path = tmp_path / 'evidence-recovery.json'
    report = {'source': str(source_path), 'database': str(target_path), 'python': sys.version,
              'sqlite': sqlite3.sqlite_version, 'turns': [], 'status': 'copying', 'copied_domains': []}
    report['source_sha256'] = {str(path.relative_to(request.config.rootpath)):
        hashlib.sha256(path.read_bytes()).hexdigest() for path in (
            Path(request.config.rootpath) / 'src/yt_playlist' / name for name in (
                'core/storage_maintenance.py', 'core/store.py', 'rec/choice_evidence.py',
                'rec/choice_retention.py', 'rec/choice_context.py', 'rec/choice_bandit.py',
                'rec/group_learning.py', 'rec/group_root_backfill.py', 'rec/group_roots.py',
                'rec/rec_worker.py', 'rec/stored_home.py', 'web/app.py'))}
    report['resumed_isolated_library'] = bool(resume)
    started = time.monotonic()
    evidence_source = source_path.with_name(source_path.name + '.evidence')
    evidence_target = target_path.with_name(target_path.name + '.evidence')
    files, digest = 0, hashlib.sha256()
    for path in sorted(evidence_source.glob('*/*/*.json')):
        relative = path.relative_to(evidence_source)
        destination = evidence_target / relative
        if not resume:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        digest.update(str(relative).encode())
        digest.update(destination.read_bytes())
        files += 1
    assert files > 1000
    # Freeze file evidence before taking the SQLite snapshot. Current producers
    # retain a publication before publishing an offer, so copied evidence cannot
    # refer to a publication created after this SQL snapshot. Do not combine an
    # older SQL donor with newer listening files from a running application.
    source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(target_path)
    target.row_factory = sqlite3.Row
    try:
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        report['schema'] = source.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
        report['recordings'] = source.execute('SELECT count(*) FROM recordings').fetchone()[0]
        report['selected_groups'] = source.execute('SELECT count(*) FROM generation_groups').fetchone()[0]
        assert report['recordings'] >= 10000 and report['selected_groups'] >= 10000
        if not resume:
            source.backup(target)
            with target:
                _copy_owned_files(source, target, source_path, target_path, report)
        else:
            previous = json.loads((target_path.parent / 'evidence-recovery.json').read_text())
            report.update(original_snapshot_source=previous['source'], owned_files=previous['owned_files'],
                          copied_domains=previous['copied_domains'])
    finally:
        target.close()
        source.close()
    report.update(copy_seconds=time.monotonic() - started, evidence_files=files,
                  copied_evidence_sha256=digest.hexdigest(), status='running')
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    store = Store(target_path)
    started = time.monotonic()
    store.init_schema()
    report['schema_validation_seconds'] = time.monotonic() - started
    ledger = store.rec_ledger.evidence()
    original_offers = ledger.records('opportunity')
    with store.read_view() as reader:
        original_contexts = {offer['context']: choice_context.correction_context(
            choice_evidence.learning_context(reader, offer['context'])) for offer in original_offers}
    original_responses = ledger.records('response')
    original_heads = list(map(tuple, store.conn.execute('SELECT * FROM group_head')))
    replay = None
    if request.config.getoption('--schema-reinventory'):
        assert resume, 'Controlled inventory replay requires an isolated retained recovery library'
        contexts = [(offer, group_roots.context(store, {'artifact': offer['context']}, ledger=ledger))
                    for offer in original_offers]
        replay, captured = max(contexts, key=lambda pair: len(group_evidence.references(pair[1])[1]))
        with group_roots.admission(store), group_roots.writer(store):
            store.conn.execute('UPDATE group_retention_state SET roots_complete=0 WHERE id=1')
            group_roots.release(store, 'offer', replay['id'])
        report['controlled_inventory_replay'] = {
            'offer': replay['id'], 'context': replay['context'],
            'referenced_versions': len(group_evidence.references(captured)[1]),
            'changed_state': 'Closed the collection fence and removed only this offer SQL owner; immutable evidence retained',
        }
    report['roots_complete_at_start'] = store.conn.execute(
        'SELECT roots_complete FROM group_retention_state').fetchone()[0]
    app = create_app(store, lambda: {}, background_work=True)
    worker = app.state.ctx.rec_worker
    monkeypatch.setattr(worker, '_start_loop', lambda: None)
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    original_acknowledge, original_context = choice_retention._acknowledge, group_roots.context

    def held_acknowledge(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(30), 'Foreground inspection did not release the evidence recovery gate'
        return original_acknowledge(*args, **kwargs)

    def held_inventory_context(owner, captured, **kwargs):
        if captured.get('artifact') == replay['context'] and not entered.is_set():
            entered.set()
            assert release.wait(30), 'Foreground inspection did not release the inventory gate'
        return original_context(owner, captured, **kwargs)

    # A resumed actual cohort can already have self-contained checkpoints and
    # need no definition lookup. Hold the real acknowledgment boundary while
    # its publication admission is owned, independently of those inputs.
    if replay is None:
        monkeypatch.setattr(choice_retention, '_acknowledge', held_acknowledge)
        report['held_recovery_phase'] = 'acknowledgment_with_publication_admission'
    else:
        monkeypatch.setattr(group_roots, 'context', held_inventory_context)
        report['held_recovery_phase'] = 'inventory_context_expansion_with_publication_admission'
    client = TestClient(app, base_url='http://127.0.0.1')
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                         lifespan='off', log_level='warning'))
    owner = LiveServer(server, startup_timeout=10)

    def recover():
        for turn in range(100):
            if stop.is_set():
                return {'deferred': True}
            started = time.monotonic()
            result = choice_evidence.maintain_storage(store, time.time())
            with store._lock:
                owners = dict(store.conn.execute('SELECT kind,count(*) FROM group_evidence_owners GROUP BY kind'))
                complete = store.conn.execute('SELECT roots_complete FROM group_retention_state').fetchone()[0]
            report['turns'].append({'turn': turn, 'seconds': time.monotonic() - started,
                'deferred': result.get('deferred', False), 'checkpoints': len(ledger.records('checkpoint')),
                'after': store.settings.get_setting(choice_retention.AFTER),
                'roots_complete': complete, 'root_owners': owners})
            report_path.write_text(json.dumps(report, indent=2) + '\n')
            if not result.get('deferred'):
                return result
        pytest.fail('Actual library recovery did not complete within 100 normal five-second admissions')

    executor = ThreadPoolExecutor(1)
    try:
        future = executor.submit(recover)
        try:
            deadline = time.monotonic() + 10
            while not client.get('/storage-maintenance/status').json()['active']:
                assert time.monotonic() < deadline, 'Recovery did not enter the paused state'
                time.sleep(.02)
            started = time.monotonic()
            initial_home = client.get('/')
            report['home_seconds_at_recovery_start'] = time.monotonic() - started
            assert initial_home.status_code == 200
            assert 'id="storage-maintenance"' not in initial_home.text
            assert 'class="gen-card-body' in initial_home.text
            # The first actual-library request includes cold Home composition.
            # Keep it separately timed from the held-admission check below;
            # neither may wait for the full recovery job to complete.
            assert report['home_seconds_at_recovery_start'] < 10
            deadline = time.monotonic() + 120
            while not entered.wait(.1):
                if future.done():
                    future.result()
                    pytest.fail('No evidence acknowledgment work was exercised')
                assert time.monotonic() < deadline
            started = time.monotonic()
            home = client.get('/')
            report['home_seconds_during_recovery'] = time.monotonic() - started
            assert home.status_code == 200
            assert 'Optimizing app storage; some recommendations are temporarily unavailable.' in home.text
            assert 'class="gen-card-body' in home.text
            assert client.get('/playlists').status_code == 200
            assert client.get('/storage-maintenance/status').json()['active']
            assert report['home_seconds_during_recovery'] < 3
            assert not future.done()
            page.goto(f'http://127.0.0.1:{port}/', wait_until='domcontentloaded')
            expect(page.locator('#storage-maintenance')).to_have_text(
                'Optimizing app storage; some recommendations are temporarily unavailable.')
            expect(page.locator('#home-mode-cards .gen-card-body').first).to_be_visible()
            page.screenshot(path=str(tmp_path / 'recovery-pending.png'), full_page=True)
        finally:
            release.set()
        future.result(600)
        assert not client.get('/storage-maintenance/status').json()['active']
        expect(page.locator('#storage-maintenance')).to_be_hidden()
        page.goto(f'http://127.0.0.1:{port}/playlists', wait_until='domcontentloaded')
        page.screenshot(path=str(tmp_path / 'recovery-complete.png'), full_page=True)
        assert list(map(tuple, store.conn.execute('SELECT * FROM group_head'))) == original_heads
        for row in original_responses:
            assert ledger.get('response', row['id']) == row
        retained = [row for row in original_offers if ledger.get('opportunity', row['id'])]
        assert retained
        with store.read_view() as reader:
            for offer in retained:
                assert choice_context.correction_context(choice_evidence.learning_context(
                    reader, offer['context'])) == original_contexts[offer['context']]
        oid = choice_evidence.issue(store, 'mixes', [{'id': 'recovered-offer'}], time.time(), context={})
        assert oid and choice_evidence.respond(store, oid, 'choice', time.time(), option_id='recovered-offer')
        worker._flush_measurements()
        assert ledger.get('opportunity', oid)
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchone()
        report['status'] = 'passed'
    finally:
        stop.set()
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)
        if report['status'] != 'passed':
            report['status'] = 'failed'
        owner.close()
        worker.shutdown()
        worker.join(30)
        client.close()
        with store._lock:
            store.conn.close()
        report_path.write_text(json.dumps(report, indent=2) + '\n')
