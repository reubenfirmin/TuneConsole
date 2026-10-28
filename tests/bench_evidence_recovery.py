"""Opt-in #473 sustained recovery audit, run only by the current test owner.

Set TUNECONSOLE_BENCH_DB to an existing isolated actual-library export. All
writes, migrations, simulated listening, publication and restore affect a new
pytest copy. Menus/catalog/model inputs come from that export; times, responses,
input perturbations and the identical-vector comparison arms are controlled.
This verifies storage and learning continuity, not recommendation superiority,
cold startup, real fitting cost or future user behavior.
"""
from copy import deepcopy
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import time
import traceback
from types import SimpleNamespace

import numpy as np
import pytest

from tests.bench_choice_retention import inventory
from tests.bench_svd_background import _backup_library
from yt_playlist.core.activity import Activity, work_budget
from yt_playlist.core.evidence_ledger import EvidenceBudgetExceeded, EvidenceLedger, _components, canonical
from yt_playlist.core.store import Store
from yt_playlist.rec import (choice_evidence as choices, choice_retention, comparison_preparation,
                            comparison_storage, group_serving, home_prepared, model_setup,
                            product_comparison as product)
from yt_playlist.rec.choice_context import checkpoint, learning_context
from yt_playlist.rec.rec_worker import RecWorker

DAY = 86400
# Test-harness allowance for 1,344 real five-minute maintenance steps plus
# pressure recovery. Production operation budgets and logical cadence are unchanged.
AUDIT_BUDGET_SECONDS = 7200


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _graph(ledger, roots):
    pending, found = list(roots), {}
    while pending:
        identity = pending.pop()
        if identity not in found:
            raw = ledger._path('artifact', identity).read_bytes()
            found[identity] = _hash(raw)
            pending.extend(_components(json.loads(raw)))
    return found


def _prepared(store):
    surfaces = ('mode_bundles', 'fresh_pool', group_serving.SURFACE, home_prepared.SURFACE)
    with store._lock:
        rows = store.conn.execute(
            "SELECT surface,payload FROM rec_proposals WHERE surface IN (?,?,?,?) "
            "OR surface LIKE 'model_pair:%' ORDER BY surface", surfaces).fetchall()
    return {name: _hash(raw.encode()) for name, raw in rows}


def _accounting(store, protected_roots, run_id):
    """Separate retained responses/restore inputs from disposable diagnostic bytes.

    Graph categories may share components; report that overlap explicitly.
    The residual includes optional diagnostics, grace leases and other records.
    """
    ledger = store.rec_ledger.evidence()
    offers, events = ledger.records('opportunity'), ledger.records('response')
    durable = {row['opportunity_id'] for row in events if row['kind'] not in {'open', 'visible'}}
    held = durable | {row['id'] for row in offers
                      if (row.get('comparison') or {}).get('run_id') == run_id}
    for format in choices.FORMATS:
        held.add((store.rec_proposals.get_proposals('choice_window:' + format) or {}).get('opportunity_id'))
    pinned = set(_graph(ledger, protected_roots))
    unacknowledged = set(_graph(ledger, {row['context'] for row in offers
                                       if not checkpoint(ledger, row['context'])}))

    def graph_bytes(identities):
        return sum(ledger._path('artifact', identity).stat().st_size for identity in identities)

    protected_records = sum(ledger._path(kind, row['id']).stat().st_size
                            for kind, rows in (('opportunity', offers), ('response', events))
                            for row in rows if (row['id'] if kind == 'opportunity'
                                                else row['opportunity_id']) in held)
    used = sum(p.stat().st_size for p in ledger.directory.glob('*/*/*.json'))
    checkpoints = sum(p.stat().st_size for p in ledger.directory.glob('checkpoint/*/*.json'))
    return {'used_bytes': used, 'protected_record_bytes': protected_records,
            'protected_artifact_bytes': graph_bytes(pinned),
            'unacknowledged_artifact_bytes': graph_bytes(unacknowledged),
            'shared_protected_unacknowledged_bytes': graph_bytes(pinned & unacknowledged),
            'checkpoint_bytes': checkpoints,
            'residual_disposable_bytes': used - protected_records - checkpoints
                                         - graph_bytes(pinned | unacknowledged)}


def _matrix_path(context):
    """Use a real captured matrix, rather than inventing a large padding string."""
    candidates = []

    def visit(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, (*path, key))
        elif (isinstance(value, list) and value and isinstance(value[0], list)
              and value[0] and isinstance(value[0][0], (int, float))):
            candidates.append((len(canonical(value)), path))

    visit(context, ())
    assert candidates, 'The actual export must contain captured numerical context'
    size, path = max(candidates)
    assert size >= 65536, 'Use an export with a representative full model context'
    return path, size


def _context(template, path, now, epoch, *, unavailable=False):
    value = deepcopy(template)
    value['at'] = now
    matrix = value
    for key in path:
        matrix = matrix[key]
    matrix[0][0] += epoch / 100_000
    if unavailable:
        # An explicitly incomplete historical definition prevents acknowledgement.
        # Its unanswered offer can still retire through the real capacity policy.
        value['musical_groups'] = [{'group_id': '473-unavailable',
                                    'version': '473-definition-not-imported'}]
    return value


def test_actual_library_sustained_capture_pressure_restart_and_restore(tmp_path, monkeypatch):
    selected = os.environ.get('TUNECONSOLE_BENCH_DB')
    if not selected:
        pytest.skip('Set TUNECONSOLE_BENCH_DB to an existing isolated library export')
    source = Path(selected).resolve()
    assert source != (Path.home() / '.local/share/yt-playlist/state.db').resolve(), (
        'Select an existing isolated export, not the native database')
    source_evidence = Path(str(source) + '.evidence')
    assert source_evidence.is_dir(), 'The export must include its external evidence'
    path, destination = tmp_path / 'library.db', tmp_path / 'sustained-recovery.json'
    clock = [time.time()]
    report = {'source': str(source), 'database': str(path), 'python': sys.version,
              'status': 'running', 'phase': 'backup', 'maintenance': [], 'days': [], 'pressure': [],
              'assumptions': {'logical_days': 14, 'listening_hours_per_day': 8,
                              'capture_seconds': 180, 'maintenance_seconds': 300,
                              'input_change_seconds': 3600, 'restart_after_days': 7,
                              'idle_periods': 'coalesced to one maintenance pass at next day start',
                              'pressure_min_captures': 200,
                              'pressure_stop': 'at least two real capacity-retirement cycles',
                              'pressure_inputs': 'new real-sized matrix and unavailable definition per capture',
                              'comparison': 'controlled identical real vectors; production paired preparation',
                              'audit_wall_budget_seconds': AUDIT_BUDGET_SECONDS,
                              'limits': 'unchanged production quota, retention, acknowledgement batch and menu limits'}}
    store = worker = None

    def save():
        destination.write_text(json.dumps(report, indent=2) + '\n')

    def open_store():
        current = Store(path)
        current.init_schema()
        current_worker = RecWorker(SimpleNamespace(store=current, work=Activity(),
            logger=logging.getLogger(__name__), now_fn=lambda: clock[0]), debounce_s=0)
        # Exercise the real queue/flush synchronously, without starting app jobs.
        monkeypatch.setattr(current_worker, '_start_loop', lambda: None)
        return current, current_worker

    def flush():
        worker._flush_measurements()

    def maintain(phase):
        started = time.monotonic()
        result = choices.maintain_storage(store, clock[0])
        ledger = store.rec_ledger.evidence()
        actual = sum(p.stat().st_size for p in ledger.directory.glob('*/*/*.json'))
        report['maintenance'].append({'phase': phase, 'at': clock[0], **result,
                                      'wall_s': time.monotonic() - started})
        assert actual == result['used_bytes'] == int((ledger.directory / '.budget').read_text())
        assert not result['full'] and actual < result['budget_bytes']
        assert result['optional_context_bytes'] <= int(
            ledger.byte_budget * choice_retention.DIAGNOSTIC_FRACTION)
        return result

    # Filesystem leases must advance with the same clock as event age checks.
    publish, put = EvidenceLedger._publish, EvidenceLedger.put

    def virtual_publish(self, target, encoded, kind, identity):
        result = publish(self, target, encoded, kind, identity)
        if kind == 'artifact':
            os.utime(target, (clock[0], clock[0]))
        return result

    def virtual_put(self, kind, identity, value, **kwargs):
        result = put(self, kind, identity, value, **kwargs)
        if kind == 'artifact':
            os.utime(self._path(kind, identity), (clock[0], clock[0]))
        return result

    monkeypatch.setattr(EvidenceLedger, '_publish', virtual_publish)
    monkeypatch.setattr(EvidenceLedger, 'put', virtual_put)
    try:
        save()
        report.update(_backup_library(source, path))
        shutil.copytree(source_evidence, Path(str(path) + '.evidence'))
        store, worker = open_store()
        ledger = store.rec_ledger.evidence()
        offers, responses = ledger.records('opportunity'), ledger.records('response')
        clock[0] = max(clock[0], *(row['observed_at'] + 1 for row in offers + responses))
        start = clock[0]
        report.update(start=start, runtime_schema=store.conn.execute(
            'SELECT MAX(version) FROM schema_migrations').fetchone()[0], before=inventory(ledger))
        report['source_hashes'] = {module.__name__: _hash(Path(module.__file__).read_bytes())
                                  for module in (choices, choice_retention, comparison_preparation,
                                                 sys.modules[EvidenceLedger.__module__])}
        templates = {format: max((row for row in offers if row['format'] == format
                                  and len(row['options'][0]) > 2),
                                 key=lambda row: row['observed_at'])['options']
                     for format in sorted(choices.FORMATS)}
        durable = {row['opportunity_id'] for row in responses
                   if row['kind'] not in {'open', 'visible'}}
        original_records = {(kind, row['id']): ledger._path(kind, row['id']).read_bytes()
                            for kind, rows in (('opportunity', offers), ('response', responses))
                            for row in rows if kind == 'response' or row['id'] in durable}
        historical = choices.replay(store, start - 1)

        report['phase'] = 'prepare-full-menus-and-comparison'
        choices.compile_report(store, start)
        for _ in range(home_prepared.MAX_MENUS + 2):
            with work_budget(30):
                home = home_prepared._prepare(store, start)
            assert home and home['menus']
            if len(home['menus']) == home_prepared.MAX_MENUS:
                break
        assert len(home['menus']) == home_prepared.MAX_MENUS
        original_vectors = store.rec_model.get_rec_vectors()
        assert original_vectors
        setup = model_setup.current_setup(store)
        changed = list(original_vectors)
        vector = np.frombuffer(changed[0][1], dtype=np.float32).copy()
        vector[0] += .001
        changed[0] = (changed[0][0], vector.tobytes())
        restore = dict(kind='product', status='applied', ran_at=start - 1,
                       previous=setup, selected=setup, grid=[], reason='Controlled restore audit')
        restore_id = store.model_selection.start_selection(start - 1, restore)
        store.settings.set_setting('rec_selection_paused', '0')
        store.model_selection.save_selection(restore_id, restore, rows=changed)
        # The copied export's old monitoring assignment is unrelated to this run.
        store.rec_proposals.put_proposals(product.MONITOR, {}, start)
        vectors = ledger.artifact({'incumbent': comparison_preparation._encode(changed),
                                   'challenger': comparison_preparation._encode(changed)})
        policy = dict(product.POLICY)
        # Follow the normal programme's error spending, including retained runs
        # from the export. Only the identical arm vectors and seed are controlled.
        with store._lock:
            attempts = max(product.state(store).get('attempt', 0), store.conn.execute(
                "SELECT COUNT(*) FROM rec_model_selection_runs "
                "WHERE json_extract(result_json,'$.kind')='product'").fetchone()[0]) + 1
        current = dict(phase='observing', attempt=attempts,
                       alpha=policy['family_alpha'] / (attempts * (attempts + 1)),
                       previous=setup, challenger=setup, policy=policy,
                       seed='473-storage-audit', constraints=product.constraints(store),
                       started_at=start, exposure_until=start + policy['horizon_days'] * DAY,
                       decision_at=start + (policy['horizon_days'] * 24 + policy['followup_hours']) * 3600,
                       vectors=vectors, generation=_hash(canonical([vectors, policy])))
        run = dict(kind='product', status='running', ran_at=start, selected=setup, grid=[], **current)
        current['run_id'] = store.model_selection.start_selection(start, run)
        store.settings.set_setting('rec_selection_live_run', str(current['run_id']))
        store.rec_proposals.put_proposals(product.SURFACE, current, start)
        # Exercise all measurement/decision fields before expensive preparation
        # or simulated days, so an incomplete controlled state fails immediately.
        preflight = product.measure(store, current, start)
        assert preflight['alpha'] == current['alpha']
        assert preflight['source_ids'] == []
        assert product.decision(current, preflight, start)[0] == 'observing'
        for format in product.FORMATS:
            selected = product.assignment(store, format, 'preflight', start)
            assert selected and selected['run_id'] == current['run_id']
            assert selected['generation'] == current['generation']
        report['comparison_preflight'] = {'attempt': attempts, 'alpha': current['alpha'],
                                          'generation': current['generation'], 'formats': list(product.FORMATS)}
        save()
        assert comparison_preparation.prepare(store, current, start)
        current = product.state(store)
        assert current['attempt'] == attempts and current['alpha'] == preflight['alpha']
        prepared = _prepared(store)
        assert sum(name.startswith(f"model_pair:{current['run_id']}:") for name in prepared) == 6
        protected_roots = {current['vectors']}
        for arm in ('incumbent', 'challenger'):
            for format in product.FORMATS:
                pair = comparison_storage.decode(store.rec_proposals.get_proposals(
                    product.pair_surface(current['run_id'], arm, format)))
                protected_roots.add((pair.get('context') or pair['choice_context'])['artifact'])
                if format == 'mixes':
                    assert len(pair['menus']) == home_prepared.MAX_MENUS
        protected_graph = _graph(ledger, protected_roots)
        template = choices.model_context(store, start)
        matrix_path, matrix_bytes = _matrix_path(template)
        report.update(context_component_bytes=matrix_bytes, restore_run=restore_id,
                      comparison_run=current['run_id'], prepared=prepared)
        maintain('before-listening')
        expected_choices = {format: 0 for format in choices.FORMATS}
        initial_counts = choices.compile_report(store, start)['formats']
        capture_count = 0
        first_choice = None
        save()
        with work_budget(AUDIT_BUDGET_SECONDS):
            for day in range(14):
                day_started = time.monotonic()
                report['phase'] = f'listening-day-{day + 1}'
                clock[0] = start + day * DAY
                maintain('day-start')
                next_maintenance = clock[0] + 300
                for offset in range(0, 8 * 3600, 180):
                    at = start + day * DAY + offset
                    while next_maintenance <= at:
                        clock[0] = next_maintenance
                        maintain('listening')
                        next_maintenance += 300
                    clock[0] = at
                    format = sorted(choices.FORMATS)[capture_count % 3]
                    options = templates[format]
                    context = _context(template, matrix_path, at, day * 8 + offset // 3600)
                    context['comparison'] = product.assignment(store, format, str(capture_count), at)
                    oid = f'sustained473:{capture_count}'
                    assert choices.issue(store, format, options, at, context=context, opportunity_id=oid) == oid
                    flush()
                    assert ledger.get('opportunity', oid), f'Queued capture was lost: {oid}'
                    if offset % 3600 == 0:
                        option = str(options[0]['id'])
                        events = [
                            ('visible', {'visible': [str(row['id']) for row in options]}),
                            ('choice', {'option_id': option, 'action_id': oid + ':pick'}),
                            ('start', {'option_id': option}),
                            ('experience', {'option_id': option, 'details': {
                                'kind': 'track_exit', 'position': 190, 'duration': 200}}),
                        ]
                        if offset == 0:
                            events.append(('feedback', {'option_id': option,
                                                        'details': {'kind': 'more'}}))
                        for delta, (kind, extra) in enumerate(events, 1):
                            clock[0] = at + delta
                            assert choices.respond(store, oid, kind, clock[0], **extra)
                            flush()
                        assert ledger.get('response', oid + ':pick')
                        expected_choices[format] += 1
                        first_choice = first_choice or (oid, format)
                    capture_count += 1
                clock[0] = start + day * DAY + 8 * 3600
                maintain('day-end')
                summary = choices.compile_report(store, clock[0])
                for format in choices.FORMATS:
                    assert summary['formats'][format]['choices'] == (
                        initial_counts[format]['choices'] + expected_choices[format])
                assert _prepared(store) == prepared
                measured = product.measure(store, current, clock[0])
                report['days'].append({'day': day + 1, 'captures': capture_count,
                                       'wall_s': time.monotonic() - day_started,
                                       'inventory': inventory(ledger), 'comparison': measured,
                                       'accounting': _accounting(store, protected_roots, current['run_id']),
                                       'expected_new_choices': dict(expected_choices)})
                if day == 6:
                    before_restart = choices.replay(store, clock[0])
                    worker.shutdown()
                    worker.join(5)
                    store.conn.close()
                    store, worker = open_store()
                    ledger = store.rec_ledger.evidence()
                    assert choices.replay(store, clock[0]) == before_restart
                    assert _prepared(store) == prepared
                    assert product.state(store) == current
                    report['restart_verified'] = True
                save()
                print(json.dumps({'report': str(destination), 'day': day + 1,
                                  'captures': capture_count,
                                  'used_bytes': report['maintenance'][-1]['used_bytes']}), flush=True)

            report['phase'] = 'unacknowledged-pressure'
            pressure_start = clock[0] + DAY
            # A fixed count need not reach 90% of an actual export's quota.
            # Bound the workload using its real component size, then stop only
            # after two measured recoveries, without reducing the production quota.
            max_pressure = max(200, (3 * ledger.byte_budget + matrix_bytes - 1) // matrix_bytes + 40)
            report['pressure_max_captures'] = max_pressure
            capacity_cycles = 0
            for index in range(max_pressure):
                clock[0] = pressure_start + index * 180
                oid = f'pressure473:{index}'
                context = _context(template, matrix_path, clock[0], 10000 + index, unavailable=True)
                assert choices.issue(store, 'radio', templates['radio'], clock[0],
                                     context=context, opportunity_id=oid) == oid
                flush()
                assert ledger.get('opportunity', oid), f'Pressure capture was lost: {oid}'
                report['pressure_captures'] = index + 1
                if index % 2 == 1:
                    result = maintain('unacknowledged-pressure')
                    capacity_cycles += result['retired_for_capacity'] > 0
                if index % 20 == 19:
                    report['pressure'].append({'captures': index + 1,
                        'accounting': _accounting(store, protected_roots, current['run_id'])})
                    save()
                if index + 1 >= 200 and capacity_cycles >= 2:
                    break
            pressure = [row for row in report['maintenance'] if row['phase'] == 'unacknowledged-pressure']
            report['capacity_recovery_cycles'] = sum(row['retired_for_capacity'] > 0 for row in pressure)
            assert report['capacity_recovery_cycles'] >= 2, (
                f'No repeated capacity recovery after {max_pressure} actual-sized captures')
            assert any(row['unacknowledged_contexts'] > 0 for row in pressure)

        report['phase'] = 'correction-and-restore'
        assert capture_count == 14 * 160
        assert _prepared(store) == prepared
        assert _graph(ledger, protected_graph) == protected_graph
        assert choices.replay(store, start - 1) == historical
        for (kind, identity), raw in original_records.items():
            assert ledger._path(kind, identity).read_bytes() == raw
        oid, format = first_choice
        before_correction = choices.compile_report(store, clock[0])
        assert choices.respond(store, oid, 'withdraw', clock[0] + 1,
                               action_id='sustained473:withdraw', details={'action_id': oid + ':pick'})
        flush()
        corrected = choices.compile_report(store, clock[0] + 2)
        assert corrected['formats'][format]['choices'] == before_correction['formats'][format]['choices'] - 1
        assert learning_context(store, ledger.get('opportunity', oid)['context'])
        assert sum(product.measure(store, current, clock[0])['overall']['days'].values()) >= 13
        store.model_selection.maintain_selection(clock[0])
        assert store.model_selection.restore_model_selection(restore_id, clock[0] + 3)
        assert dict(store.rec_model.get_rec_vectors()) == dict(original_vectors)
        late = [day['accounting']['residual_disposable_bytes'] for day in report['days'][7:]]
        report['normal_storage'] = {
            'peak_bytes': max(row['used_bytes'] for row in report['maintenance']
                              if row['phase'] != 'unacknowledged-pressure'),
            'protected_record_growth_bytes': (report['days'][-1]['accounting']['protected_record_bytes']
                                             - report['days'][0]['accounting']['protected_record_bytes']),
            'last_week_residual_min_bytes': min(late), 'last_week_residual_max_bytes': max(late),
        }
        report.update(status='passed', captures=capture_count, after=inventory(ledger),
                      historical_replay_preserved=True, protected_records_preserved=True,
                      correction_verified=True, exact_restore_verified=True)
    except BaseException as error:
        report.update(status='failed', error={'type': type(error).__name__, 'message': str(error),
                                              'traceback': traceback.format_exc()})
        raise
    finally:
        save()
        print(json.dumps({'report': str(destination), 'status': report['status'],
                          'phase': report['phase']}), flush=True)
        if worker is not None:
            worker.shutdown()
            worker.join(5)
        if store is not None:
            store.conn.close()


def test_protected_full_storage_explicitly_refuses_capture_without_losing_evidence(store, tmp_path, monkeypatch):
    """A controlled small quota checks the honest limit, separate from the real-data audit."""
    ledger = EvidenceLedger(tmp_path / 'protected-full', 256 * 1024)
    monkeypatch.setattr(store.rec_ledger, 'evidence', lambda: ledger)
    assert choices.issue(store, 'radio', [{'id': 'a'}, {'id': 'b'}], 100,
                         context={'held': 'x' * 100_000}, opportunity_id='held') == 'held'
    assert choices.respond(store, 'held', 'choice', 101, option_id='a')
    offer = ledger.get('opportunity', 'held')
    store.rec_proposals.put_proposals(home_prepared.SURFACE,
        {'choice_context': {'artifact': offer['context']}}, 100)
    before = {p.relative_to(ledger.directory): p.read_bytes()
              for p in ledger.directory.glob('*/*/*.json')}
    ledger.byte_budget = sum(len(raw) for raw in before.values())
    result = choices.maintain_storage(store, 2000)
    assert result['full'] and result['retired_for_capacity'] == 0
    with pytest.raises(EvidenceBudgetExceeded):
        choices.issue(store, 'radio', [{'id': 'new'}], 2001, context={}, opportunity_id='refused')
    assert ledger.get('opportunity', 'refused') is None
    for relative, raw in before.items():
        assert (ledger.directory / relative).read_bytes() == raw
