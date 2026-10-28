"""Opt-in #138 lifecycle audit. Every write targets a disposable library backup.

Run with uv run pytest -s tests/bench_choice_retention.py. TUNECONSOLE_BENCH_DB
may select an existing isolated actual-library export to avoid a full live backup.
The listening responses and time progression below are controlled audit inputs,
not observations of future user behavior or evidence of model superiority.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import traceback

from tests.bench_svd_background import _backup_library, _saved_library_source
from yt_playlist.core.numerics import limit_native_threads
from yt_playlist.core.activity import work_budget
from yt_playlist.core.store import Store
from yt_playlist.rec import (choice_evidence as choices, comparison_preparation,
                            comparison_storage, model_refresh,
                            candidate_refresh, home_prepared, group_serving, rec_params,
                            product_comparison as product)


def inventory(ledger):
    return {kind: {'records': len(paths), 'bytes': sum(path.stat().st_size for path in paths)}
            for kind in ('opportunity', 'response', 'artifact', 'checkpoint', 'report')
            for paths in [list((ledger.directory / kind).glob('*/*.json'))]}


def retention_snapshot(store, cutoff):
    """Keep exact inputs identifiable without retaining decoded model graphs."""
    ledger = store.rec_ledger.evidence()
    records = {}
    for kind in ('opportunity', 'response', 'checkpoint'):
        records[kind] = {}
        for path in sorted((ledger.directory / kind).glob('*/*.json')):
            raw = path.read_bytes()
            record = json.loads(raw)
            identity = record['source_context'] if kind == 'checkpoint' else record['id']
            records[kind][identity] = {
                'sha256': hashlib.sha256(raw).hexdigest(),
                **{key: record[key] for key in ('observed_at', 'at', 'kind', 'format',
                    'opportunity_id', 'source_context') if key in record},
            }
            if kind == 'opportunity':
                records[kind][identity]['context'] = record['context']
            if kind == 'response' and record['kind'] == 'withdraw':
                records[kind][identity]['action_id'] = record['details'].get('action_id')
    return {'database': str(store._db_path), 'evidence': str(ledger.directory.resolve()),
            'inventory': inventory(ledger), 'records': records,
            'replay': choices.replay(store, cutoff)}


def retention_difference(before, after):
    records = {}
    for kind, original in before['records'].items():
        current = after['records'][kind]
        records[kind] = {
            'missing': sorted(original.keys() - current.keys()),
            'added': sorted(current.keys() - original.keys()),
            'changed': sorted(identity for identity in original.keys() & current.keys()
                              if original[identity] != current[identity]),
        }
    original, current = before['replay'], after['replay']
    return {'same_location': (before['database'], before['evidence']) ==
                            (after['database'], after['evidence']),
            'records': records,
            'missing_source_ids': sorted(set(original['source_ids']) - set(current['source_ids'])),
            'added_source_ids': sorted(set(current['source_ids']) - set(original['source_ids'])),
            'different_formats': {format: {'before': original['formats'][format],
                                           'after': current['formats'][format]}
                                  for format in choices.FORMATS
                                  if original['formats'][format] != current['formats'][format]}}


def test_actual_library_report_refresh_and_unanswered_retirement_keep_full_menus(tmp_path):
    source, path = _saved_library_source(), tmp_path / 'library.db'
    _backup_library(source, path)
    evidence = Path(str(source) + '.evidence')
    if evidence.exists():
        shutil.copytree(evidence, Path(str(path) + '.evidence'))
    store = Store(path)
    store.init_schema()
    try:
        now = time.time()
        choices.compile_report(store, now)
        # Begin without prepared Home menus; complete source pools are retained.
        # This proves incremental Home startup, not cold catalog/model processing.
        store.rec_proposals.put_proposals(home_prepared.SURFACE, {}, now)
        pools = ('mode_bundles', 'fresh_pool', group_serving.SURFACE)
        before = {name: store.rec_proposals._proposal_json(name) for name in pools}
        adaptive = group_serving.checkpoint(store)
        bundles = store.rec_proposals.group_pools_json() if adaptive else None
        generation = home_prepared._proposal_generation(store, bundles, now)
        preferences = candidate_refresh.preferences(store, now)
        attempts = []
        # This outer budget isolates history reuse from cold preparation latency;
        # the normal per-batch menu stopping condition remains in effect.
        for index in range(home_prepared.MAX_MENUS + 2):
            # Hold logical time fixed to isolate history changes from genuine
            # hourly preference decay or a historical learning window expiring.
            current, expired = f'admission-audit:{index}', f'expired-audit:{index}'
            old = now - (rec_params.get_param(store, 'choice_retention_d') + 1) * product.DAY
            for oid, offered_at in ((current, now), (expired, old)):
                assert choices.issue(store, 'albums', [{'id': oid}], offered_at,
                                     context={}, opportunity_id=oid) == oid
            choices.refresh_report(store, now)
            choices.maintain_storage(store, now)
            assert store.rec_ledger.evidence().get('opportunity', expired) is None
            assert store.rec_ledger.evidence().get('opportunity', current)
            choices.refresh_report(store, now)
            assert home_prepared._proposal_generation(store, bundles, now) == generation
            with work_budget(30):
                payload = home_prepared._prepare(store, now)
            assert payload and payload['menus']
            if attempts:
                assert len(payload['menus']) >= attempts[-1]
            attempts.append(len(payload['menus']))
            assert before == {name: store.rec_proposals._proposal_json(name) for name in pools}
            assert candidate_refresh.preferences(store, now) == preferences
        assert attempts[-1] == home_prepared.MAX_MENUS
        assert {menu['epoch'] for menu in payload['menus']} == set(range(home_prepared.MAX_MENUS))
        print(json.dumps({'menus_per_history_cycle': attempts, 'target': home_prepared.MAX_MENUS,
                          'unchanged_generation': generation, 'pools_unchanged': True,
                          'candidate_preferences_unchanged': True,
                          'controlled_clock': now, 'source': str(source)}), flush=True)
    finally:
        store.conn.close()


def test_actual_library_full_trial_retention_correction_restart_and_restore(tmp_path, monkeypatch):
    source, path = _saved_library_source(), tmp_path / 'library.db'
    selected_report = os.environ.get('TUNECONSOLE_RETENTION_REPORT')
    destination = (Path(selected_report) if selected_report else
                   Path(tempfile.mkdtemp(prefix='tuneconsole-choice-retention-')) / 'audit.json')
    report = {'source': str(source.resolve()), 'database': str(path.resolve()),
              'python': sys.version, 'status': 'running', 'phase': 'backup',
              'controlled_inputs': '28 simulated daily listening responses and clock advances',
              'checkpoints': [], 'series': []}
    store, original_snapshot = None, None

    def save():
        destination.write_text(json.dumps(report, indent=2) + '\n')

    def checkpoint(phase, *, compare=True):
        report['phase'] = phase
        state = retention_snapshot(store, now + 5)
        state['phase'] = phase
        report['checkpoints'].append(state)
        if original_snapshot is not None and compare:
            state['difference'] = retention_difference(original_snapshot, state)
        save()
        if original_snapshot is not None and compare:
            assert state['difference']['same_location'], state['difference']
            assert state['replay'] == original_snapshot['replay'], state['difference']
        return state

    try:
        save()
        print(json.dumps({'retention_report': str(destination)}), flush=True)
        report.update(_backup_library(source, path))
        evidence = Path(str(source) + '.evidence')
        report['source_evidence'] = str(evidence.resolve())
        if evidence.exists():
            shutil.copytree(evidence, Path(str(path) + '.evidence'))
        limit_native_threads()
        store = Store(path)
        store.init_schema()
        report['runtime_schema'] = store.conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
        report['source_files_at_start'] = {
            module.__name__: {'path': str(Path(module.__file__).resolve()),
                             'sha256': hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
            for module in (choices, comparison_preparation, model_refresh,
                           sys.modules[Store.__module__],
                           sys.modules['yt_playlist.core.evidence_ledger'])}
        report['before'] = inventory(store.rec_ledger.evidence())
        now = time.time()
        report['clock'] = now
        checkpoint('before-training', compare=False)
        store.settings.set_setting('rec_selection_paused', '0')
        store.rec_proposals.put_proposals(product.SURFACE, {}, now)
        # A real ordinary training pass is separate from the trial's frozen fit.
        report['phase'] = 'initial-training'
        save()
        store.settings.delete_setting('rec_fit_inputs')
        assert model_refresh.rebuild(store, now) is not None
        checkpoint('after-training', compare=False)
        incumbent = store.rec_model.get_rec_vectors()
        report['phase'] = 'initial-comparison'
        save()
        # This audit exercises retained legacy menu experiments explicitly;
        # daily prediction selection now has a separate production entrypoint.
        comparison_preparation.maintain(store, now)
        checkpoint('after-initial-comparison', compare=False)
        current = product.state(store)
        assert current['phase'] == 'observing', current
        horizon = current['decision_at']
        report['trial'] = {key: current[key] for key in ('run_id', 'vectors', 'generation', 'decision_at')}
        series = report['series']
        for day in range(28):
            at = now + day * product.DAY
            if day:
                report['phase'] = f'day-{day}-preparation'
                save()
                comparison_preparation.maintain(store, at)
                checkpoint(f'day-{day}-after-preparation')
            current = product.state(store)
            assert current['phase'] == 'observing' and current['prepared_at'] == at, current
            assert current['decision_at'] == horizon
            pair_sizes, coverage = 0, {}
            for arm in ('incumbent', 'challenger'):
                coverage[arm] = {}
                for format in product.FORMATS:
                    raw = store.rec_proposals._proposal_json(product.pair_surface(current['run_id'], arm, format))
                    pair_sizes += len(raw.encode())
                    data = comparison_storage.decode(json.loads(raw))
                    coverage[arm][format] = (len(data['menus']) if format == 'mixes' else
                        len(data['items']) if format == 'albums' else
                        len(set().union(*(set(keys) for keys in data['bundles'].get('_radio', {}).get('members', {}).values()))))
                assert coverage[arm]['mixes'] == 12
                assert coverage[arm]['radio'] >= 100
            assert pair_sizes <= comparison_preparation.MAX_PREPARED_BYTES
            data, assigned = product.prepared(store, 'mixes', f'audit:{day}', at)
            protos = data['menus'][day % len(data['menus'])]['protos']
            assert protos
            options = [{**proto, 'id': str(index), 'score': float(-index)} for index, proto in enumerate(protos)]
            oid = f'retention-audit:{day}'
            assert choices.issue(store, 'mixes', options, at,
                                 context={**data['choice_context'], 'comparison': assigned}, opportunity_id=oid) == oid
            choices.respond(store, oid, 'visible', at + 1, visible=[o['id'] for o in options])
            choices.respond(store, oid, 'choice', at + 2, option_id='0', action_id=oid + ':pick')
            choices.respond(store, oid, 'start', at + 3, option_id='0')
            choices.respond(store, oid, 'experience', at + 4, option_id='0', details={
                'kind': 'track_exit', 'position': 190, 'duration': 200})
            before_cleanup = choices.replay(store, at + 5)
            daily = {'day': day, 'pair_bytes': pair_sizes, 'coverage': coverage,
                     'replay_before_cleanup': before_cleanup}
            series.append(daily)
            if day == 0:
                original_snapshot = checkpoint('day-0-baseline', compare=False)
                report['original_replay'] = original_snapshot['replay']
                save()
            ledger = store.rec_ledger.evidence()
            # Clock advancement makes these captures older than the in-flight grace.
            for artifact in ledger.directory.glob('artifact/*/*.json'):
                os.utime(artifact, (at - 1000, at - 1000))
            storage = choices.maintain_storage(store, at + 1000)
            daily.update(storage=storage, inventory=inventory(ledger),
                         replay_after_cleanup=choices.replay(store, at + 5))
            checkpoint(f'day-{day}-after-maintenance')
            assert storage['optional_context_bytes'] <= ledger.byte_budget * .1
            assert storage['used_bytes'] <= ledger.byte_budget
            assert daily['replay_after_cleanup'] == before_cleanup
            measured = product.measure(store, current, at + product.DAY + 1000)
            assert sum(measured['overall']['days'].values()) >= 1
            if day == 14:
                checkpoint('before-mid-trial-training')
                report['phase'] = 'mid-trial-training'
                save()
                store.settings.delete_setting('rec_fit_inputs')
                assert model_refresh.rebuild(store, at + 1100) is not None
                checkpoint('before-store-reopen')
                store.conn.close()
                store = Store(path)
                store.init_schema()
                checkpoint('after-store-reopen')
        # A failed daily publication keeps every last-good paired byte intact.
        current = product.state(store)
        vectors = store.rec_ledger.evidence().get('artifact', current['vectors'])
        previous_pairs = list(store.conn.execute("SELECT surface,payload FROM rec_proposals WHERE surface LIKE 'model_pair:%' ORDER BY surface"))
        with monkeypatch.context() as failure:
            failure.setattr(comparison_preparation, 'MAX_PREPARED_BYTES', 1)
            assert not comparison_preparation.prepare(store, current, now + 27 * product.DAY + 2000)
        assert list(store.conn.execute("SELECT surface,payload FROM rec_proposals WHERE surface LIKE 'model_pair:%' ORDER BY surface")) == previous_pairs
        checkpoint('after-failed-publication')
        report['phase'] = 'comparison-decision'
        save()
        comparison_preparation.maintain(store, horizon)
        checkpoint('after-comparison-decision')
        assert product.state(store)['phase'] != 'observing'
        report['decision'] = store.model_selection.selection_run(current['run_id'])
        comparison_before = product.measure(store, current, horizon)
        # Release all optional diagnostic graphs after their age limit. Saved
        # predictions and withdrawal cutoffs remain reproducible after restart.
        cutoff = now + 61 * product.DAY
        checkpoint('before-61-day-maintenance')
        report['phase'] = '61-day-maintenance'
        save()
        report['final_storage'] = choices.maintain_storage(store, cutoff)
        checkpoint('after-61-day-maintenance')
        assert product.measure(store, current, horizon) == comparison_before
        count_before = choices.compile_report(store, cutoff)['formats']['mixes']['choices']
        choices.respond(store, 'retention-audit:0', 'withdraw', cutoff + 1,
                        details={'action_id': 'retention-audit:0:pick'})
        corrected = choices.compile_report(store, cutoff + 2)
        assert corrected['formats']['mixes']['choices'] == count_before - 1
        checkpoint('after-withdrawal')
        # Controlled publication exercises exact rollback independently of whether
        # this simulated trial has enough statistical support for promotion.
        run = {'ran_at': cutoff, 'status': 'applied', 'kind': 'product', 'previous': current['previous'],
               'selected': current['challenger'], 'reason': 'Controlled restore audit', 'grid': []}
        run_id = store.model_selection.start_selection(cutoff, run)
        restore_expected = store.rec_model.get_rec_vectors()
        store.model_selection.save_selection(run_id, run, rows=comparison_preparation._rows(vectors['challenger']))
        store.model_selection.maintain_selection(cutoff + 400 * product.DAY)
        assert store.model_selection.restore_model_selection(run_id, cutoff + 401 * product.DAY)
        assert store.rec_model.get_rec_vectors() == restore_expected
        report.update(after=inventory(store.rec_ledger.evidence()),
                      initial_vector_digest=hashlib.sha256(b''.join(v for _, v in incumbent)).hexdigest(),
                      restored=True, comparison_failure_preserved_last_good=True)
        checkpoint('after-exact-restore')
        report['status'] = 'passed'
    except BaseException as error:
        report.update(status='failed', error={'type': type(error).__name__, 'message': str(error),
                                              'traceback': traceback.format_exc()})
        raise
    finally:
        try:
            save()
            print(json.dumps({'retention_report': str(destination), 'status': report['status'],
                              'phase': report['phase']}), flush=True)
        finally:
            if store is not None:
                store.conn.close()
