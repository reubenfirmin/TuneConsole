"""Opt-in #138 cost/coverage audit on a read-only backup of a real saved library.

uv run pytest -s tests/bench_choice_evaluation.py
This creates no live exposure, promotion or purported future outcomes. Every write
and recovery exercise is confined to pytest's disposable copy.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import faulthandler
import hashlib
import json
import logging
from pathlib import Path
import resource
import subprocess
import sys
import time

import pytest

from tests.bench_svd_background import _backup_library, _saved_library_source
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.core.numerics import limit_native_threads
from yt_playlist.core.store import Store
from yt_playlist.rec import comparison_preparation, daily_model_selection, model_selection as selection
from yt_playlist.rec.comparison_storage import decode


def test_saved_library_production_comparison_preparation(tmp_path):
    """Exercise production candidate and serving preparation on a read-only backup."""
    backup = tmp_path / 'library.db'
    report = _backup_library(_saved_library_source(), backup)
    command = [sys.executable, '-m', 'tests.bench_choice_evaluation', str(backup)]
    # A stuck executor/loop must not hold the review test lock indefinitely.
    # The child owns only this disposable database; timeout kills and reaps it.
    try:
        child = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                               capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired as exc:
        (tmp_path / 'comparison-stdout.log').write_bytes(exc.stdout or b'')
        (tmp_path / 'comparison-stderr.log').write_bytes(exc.stderr or b'')
        pytest.fail(f'Comparison audit exceeded 240 seconds; diagnostics in {tmp_path}')
    (tmp_path / 'comparison-stdout.log').write_text(child.stdout)
    (tmp_path / 'comparison-stderr.log').write_text(child.stderr)
    destination = tmp_path / 'production-comparison.json'
    print(json.dumps({'report': str(destination), 'backup': report,
                      'returncode': child.returncode}), flush=True)
    assert child.returncode == 0, f'{child.stdout}\n{child.stderr}'
    print(destination.read_text(), flush=True)


async def _concurrent_preparation(store, now, report):
    lag, reads = [], []

    def timed_read():
        started = time.monotonic()
        store.rec_model.rec_vectors_count()
        return time.monotonic() - started

    with ThreadPoolExecutor(2, thread_name_prefix='comparison-audit') as workers:
        task = workers.submit(comparison_preparation.maintain, store, now)
        read = None
        # Poll raw worker futures on an event-loop timer. Completion/failure no
        # longer depends on a thread-to-loop completion callback waking a waiter.
        while not task.done() or read is not None:
            if read is None:
                read = workers.submit(timed_read)
            due = time.monotonic() + .05
            await asyncio.sleep(.05)
            lag.append(max(0, time.monotonic() - due))
            if read.done():
                reads.append(read.result())  # Propagate foreground failures too.
                read = None
        report.update(event_loop_samples=len(lag), event_loop_max_ms=max(lag, default=0) * 1000,
                      concurrent_read_samples=len(reads), concurrent_read_max_ms=max(reads, default=0) * 1000)
        return task.result()


def _production_comparison_preparation(backup, report, monkeypatch):
    from yt_playlist.rec import comparison_preparation, comparison_storage, model_setup, product_comparison
    limit_native_threads()
    store = Store(backup)
    store.init_schema()
    try:
        original_encode, encoded_values, encoded_payloads = comparison_storage.encode, [], []

        def checked_encode(run_id, data):
            value = original_encode(run_id, data)
            payload = json.loads(value)
            before = hashlib.sha256(canonical(data)).hexdigest()
            encoded_payloads.append(payload)
            encoded_values.append({key: payload[key] for key in ('codec', 'expanded_bytes', 'packed_bytes')})
            encoded_values[-1].update(stored_bytes=len(value.encode()), sha256=before)
            return value

        monkeypatch.setattr(comparison_storage, 'encode', checked_encode)
        report['encoded_values'] = encoded_values
        # These resets affect only the disposable copy, never the source library.
        store.settings.set_setting('rec_selection_paused', '0')
        store.conn.execute("DELETE FROM rec_proposals WHERE surface='model_live_comparison'")
        store.conn.commit()
        previous = store.rec_model.get_rec_vectors()
        now, started = time.time(), time.monotonic()
        result = asyncio.run(_concurrent_preparation(store, now, report))
        report['wall_seconds'] = time.monotonic() - started
        report['result'] = result
        print(json.dumps({'preparation': result}), flush=True)
        assert result and result['status'] == 'running' and result['phase'] == 'observing', report
        assert previous == store.rec_model.get_rec_vectors()
        current = product_comparison.state(store)
        report['projected_bytes'] = sum(len(row[0]) for row in store.conn.execute(
            "SELECT payload FROM rec_proposals WHERE surface LIKE ?", (f"model_pair:{current['run_id']}:%",)))
        assert report['projected_bytes'] <= comparison_preparation.MAX_PREPARED_BYTES, report
        assert len(encoded_values) == 6, report
        report['preparation_timings'] = store.model_selection.selection_run(current['run_id'])['preparation_timings']
        report['evidence_bytes'] = sum(path.stat().st_size for path in store.rec_ledger.evidence().directory.glob('*/*/*.json'))
        report['coverage'], pairs = {}, {}
        expected_values = iter(encoded_values)
        for arm in ('incumbent', 'challenger'):
            report['coverage'][arm], pairs[arm] = {}, {}
            for format in product_comparison.FORMATS:
                data = decode(store.rec_proposals.get_proposals(product_comparison.pair_surface(current['run_id'], arm, format)))
                expected = next(expected_values)
                assert hashlib.sha256(canonical(data)).hexdigest() == expected['sha256']
                pairs[arm][format] = data
                report['coverage'][arm][format] = (len(data['menus']) if format == 'mixes' else len(data['items'])
                    if format == 'albums' else sum(map(len, data['bundles'].get('_radio', {}).get('members', {}).values())))
        report['observed'] = product_comparison.measure(store, current, now)['overall']['days']
        old, new = pairs['incumbent'], pairs['challenger']
        mix_tracks = lambda data: [[[t['key'] for t in proto['tracks']] for proto in menu['protos']]
                                  for menu in data['menus']]
        old_scores = {row['browse_id']: row['score'] for row in old['albums']['items']}
        report['actual_serving_changes'] = {
            'mix_menus': sum(a != b for a, b in zip(mix_tracks(old['mixes']), mix_tracks(new['mixes']))),
            'album_order': [r['browse_id'] for r in old['albums']['items']] != [r['browse_id'] for r in new['albums']['items']],
            'album_scores': sum(old_scores.get(row['browse_id']) != row['score'] for row in new['albums']['items']),
            'radio_ranked_clusters': sum(keys != new['radio']['bundles']['_radio']['members'].get(mid)
                for mid, keys in old['radio']['bundles']['_radio']['members'].items())}
        # Daily refresh uses both original vector snapshots with fresh shared
        # inputs, without resetting assignment, uncertainty or the fixed horizon.
        monkeypatch.setattr(model_setup, 'build', lambda *a: pytest.fail('daily refresh refitted a model'))
        started = time.monotonic()
        comparison_preparation.maintain(store, now + product_comparison.DAY)
        report['daily_refresh_seconds'] = time.monotonic() - started
        refreshed = product_comparison.state(store)
        assert refreshed['prepared_at'] == now + product_comparison.DAY, refreshed
        assert refreshed['generation'] == current['generation']
        assert refreshed['decision_at'] == current['decision_at']
        assert store.rec_model.get_rec_vectors() == previous
        assert len(encoded_values) == 12, report
        for payload, expected in zip(encoded_payloads[6:], encoded_values[6:]):
            assert hashlib.sha256(canonical(decode(payload))).hexdigest() == expected['sha256']
        report['daily_refresh_timings'] = store.model_selection.selection_run(current['run_id'])['preparation_timings']
        report['peak_process_rss_mib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if sys.platform == 'darwin' else 1024) / 1024 ** 2
    finally:
        destination = backup.parent / 'production-comparison.json'
        destination.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'report': str(destination), **report}), flush=True)
        store.conn.close()


def test_saved_library_album_menu_frontier(tmp_path):
    """Audit the revised album policy on a disposable copy, without live exposure."""
    from yt_playlist.rec import album_tuning
    backup = tmp_path / 'library.db'
    report = _backup_library(_saved_library_source(), backup)
    limit_native_threads()
    store = Store(backup)
    store.init_schema()
    try:
        now, started = time.time(), time.monotonic()
        prepared = album_choices.prepare_cached(store, now)
        assert prepared and prepared['items']
        report['prepare_wall_s'] = time.monotonic() - started
        pool = album_choices.eligible(store, prepared['items'], now)
        report['frontier'] = {'options': len(pool),
            'scored': sum(row.get('score') is not None for row in pool),
            'unsaved': sum(row.get('classification') in {'discovery', 'familiar_artist'} for row in pool)}
        report['menus'] = []
        for epoch in range(4):
            items, policy = album_choices.menu(pool, album_tuning.defaults(store), epoch)
            assert len(items) == len({row['artist'].casefold() for row in items}) == 4
            assert all(row.get('score') is not None for row in items)
            report['menus'].append({'epoch': epoch,
                'discovery_requested': policy['discovery_requested'],
                'discovery_offered': policy['discovery_offered'], 'coverage_gap': policy['coverage_gap'],
                'records': [{key: row.get(key) for key in ('artist', 'title', 'year', 'genre',
                             'classification', 'score', 'interest', 'affinity')} for row in items]})
        destination = tmp_path / 'album-menu.json'
        destination.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        print(json.dumps({'report': str(destination), **report}, ensure_ascii=False), flush=True)
    finally:
        store.conn.close()


def test_saved_library_separate_home_preparation_budgets(tmp_path, caplog):
    """Albums must not consume the two-second Mix tapes publication deadline."""
    from yt_playlist.rec import home_prepared
    caplog.set_level(logging.DEBUG, logger='yt_playlist.rec.home_prepared')
    backup = tmp_path / 'library.db'
    report = _backup_library(_saved_library_source(), backup)
    limit_native_threads()
    store = Store(backup)
    store.init_schema()
    try:
        now = time.time()
        started = time.monotonic()
        assert choice_evidence.prepare_context(store, now)
        report['context_wall_s'] = time.monotonic() - started
        started = time.monotonic()
        albums = album_choices.prepare_cached(store, now)
        report['album_wall_s'] = time.monotonic() - started
        assert albums is not None and albums['items']
        started = time.monotonic()
        mixes = home_prepared.prepare(store, now)
        report['mixes_wall_s'] = time.monotonic() - started
        assert mixes is not None and mixes['menus']
        assert mixes['album_choices'] == albums['items']
        assert mixes['album_choice_context'] == albums['context']
        report['album_options'] = len(albums['items'])
        report['mix_menus'] = len(mixes['menus'])
        started = time.monotonic()
        assert choice_evidence.prepare_context(store, now + 1)
        report['following_context_wall_s'] = time.monotonic() - started
        started = time.monotonic()
        assert album_choices.prepare_cached(store, now + 1) == albums
        report['album_reuse_wall_s'] = time.monotonic() - started
        started = time.monotonic()
        following = home_prepared.prepare(store, now + 1)
        report['following_mixes_wall_s'] = time.monotonic() - started
        assert following is not None and len(following['menus']) > len(mixes['menus'])
        report['home_cache_bytes'] = len(store.rec_proposals._proposal_json('home_prepared').encode())
        report['evidence_bytes'] = sum(path.stat().st_size for path in
            store.rec_ledger.evidence().directory.glob('*/*/*.json'))
        destination = tmp_path / 'home-preparation.json'
        destination.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'report': str(destination), **report}), flush=True)
    finally:
        store.conn.close()


def test_saved_library_choice_evaluation_cost_and_coverage(tmp_path):
    backup = tmp_path / 'library.db'
    report = _backup_library(_saved_library_source(), backup)
    report['source'] = str(_saved_library_source())
    command = [sys.executable, '-m', 'tests.bench_choice_evaluation', str(backup), '--daily']
    try:
        child = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                               capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        (tmp_path / 'daily-stdout.log').write_bytes(exc.stdout or b'')
        (tmp_path / 'daily-stderr.log').write_bytes(exc.stderr or b'')
        pytest.fail(f'Daily selection exceeded 300 seconds; diagnostics in {tmp_path}')
    (tmp_path / 'daily-stdout.log').write_text(child.stdout)
    (tmp_path / 'daily-stderr.log').write_text(child.stderr)
    destination = tmp_path / 'daily-comparison.json'
    print(json.dumps({'report': str(destination), 'backup': report,
                      'returncode': child.returncode}), flush=True)
    assert child.returncode == 0, f'{child.stdout}\n{child.stderr}'
    print(destination.read_text(), flush=True)


def _daily_comparison(backup, monkeypatch):
    """Exercise actual daily fits without inventing activity or lowering gates."""
    limit_native_threads()
    store = Store(backup)
    store.init_schema()
    report, fits, reads, lag = {'python': sys.version, 'copy': str(backup)}, [], [], []
    report['sources'] = {
        module.__name__: {'path': str(Path(module.__file__).resolve()),
                         'sha256': hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
        for module in (daily_model_selection, selection, selection.training,
                       sys.modules[Store.__module__])}
    original_fit = selection.training.build_vectors_from_baskets

    def fit(baskets, **kwargs):
        started, cpu = time.monotonic(), time.thread_time()
        try:
            return original_fit(baskets, **kwargs)
        finally:
            fits.append({'method': kwargs.get('method'), 'dim': kwargs.get('dim'),
                         'baskets': len(baskets), 'wall_s': time.monotonic() - started,
                         'cpu_s': time.thread_time() - cpu})

    def vector_digest():
        digest = hashlib.sha256()
        for key, value in sorted(store.rec_model.get_rec_vectors()):
            digest.update(key.encode() + b'\0' + value)
        return digest.hexdigest()

    async def step(now):
        def timed_read():
            started = time.monotonic()
            store.rec_model.rec_vectors_count()
            return time.monotonic() - started

        with ThreadPoolExecutor(2, thread_name_prefix='daily-audit') as workers:
            task = workers.submit(daily_model_selection.maintain, store, now)
            read = None
            while not task.done() or read is not None:
                if read is None:
                    read = workers.submit(timed_read)
                due = time.monotonic() + .02
                await asyncio.sleep(.02)
                lag.append(max(0, time.monotonic() - due))
                if read.done():
                    reads.append(read.result())
                    read = None
            return task.result()

    monkeypatch.setattr(selection.training, 'build_vectors_from_baskets', fit)
    started = time.monotonic()
    try:
        # Only the disposable copy is unpaused and admitted immediately. History,
        # outcomes, setups and all minimum-activity thresholds remain untouched.
        store.settings.set_setting('rec_selection_paused', '0')
        store.settings.set_setting('rec_selection_restore', '')
        store.rec_proposals.put_proposals(daily_model_selection.SCHEDULE, {}, time.time())
        now = time.time()
        report['controlled_copy_changes'] = ['unpause selection', 'clear pending restore', 'clear poll delay']
        report['readiness'] = daily_model_selection.readiness(store, now)
        with store.read_view() as reader:
            captured, _ = daily_model_selection._capture(reader, now)
        report['historical_comparison'] = {
            'cutoff': captured['cutoff'], 'end': captured['end'], 'periods': 1,
            'baskets': len(captured['baskets']), 'context_tracks': len(captured['context']),
            'eligible_tracks': len(captured['eligible']), 'first_play_targets': len(captured['held']),
            'active_days': len(set(captured['held'].values())), 'coverage': captured['coverage'],
            'unavailable_targets': captured['unavailable_targets'],
            'generated_excluded': captured['generated_excluded']}
        del captured
        before_vectors, before_setup = vector_digest(), selection.current_setup(store)
        result = None
        steps = []
        for _ in range(len(daily_model_selection.model_setup.CONFIGS) + 8):
            at = time.monotonic()
            result = asyncio.run(step(now))
            steps.append({'poll_at': now, 'wall_s': time.monotonic() - at,
                          'status': result['status'] if result else 'waiting'})
            if not result or result['status'] != 'running':
                break
            # Advance only the scheduler clock to its saved retry time. The
            # holdout end remains fixed; no simulated listening is introduced.
            now = max(time.time(), store.rec_proposals.get_proposals(daily_model_selection.SCHEDULE)['next_at'])
        report.update(selection=result, steps=steps, fits=fits,
                      workflow_completed=bool(result and result['status'] in ('applied', 'retained')))
        assert not result or result['status'] != 'running', report
        assert not result or result['status'] != 'failed', report
        if not report['workflow_completed']:
            assert vector_digest() == before_vectors
            assert selection.current_setup(store) == before_setup
            report['acceptance_gap'] = 'Actual history did not admit a complete comparison; switching remains unverified.'
        elif result['status'] == 'applied':
            assert store.model_selection.restore_model_selection(result['id'], now + 1)
            assert vector_digest() == before_vectors
            assert selection.current_setup(store) == before_setup
            report['exact_restore_verified'] = True
        report.update(wall_s=time.monotonic() - started,
            foreground_samples=len(reads), foreground_max_ms=max(reads, default=0) * 1000,
            foreground_p95_ms=sorted(reads)[min(len(reads) - 1, int(len(reads) * .95))] * 1000 if reads else None,
            foreground_p99_ms=sorted(reads)[min(len(reads) - 1, int(len(reads) * .99))] * 1000 if reads else None,
            event_loop_max_ms=max(lag, default=0) * 1000,
            peak_process_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
                1 if sys.platform == 'darwin' else 1024) / 1024 ** 2)
        assert report['foreground_max_ms'] < 1000, report
        assert report['event_loop_max_ms'] < 250, report
    finally:
        destination = backup.parent / 'daily-comparison.json'
        destination.write_text(json.dumps(report, indent=2) + '\n')
        store.conn.close()


if __name__ == '__main__':
    faulthandler.dump_traceback_later(90, repeat=True)
    with pytest.MonkeyPatch.context() as patch:
        if '--daily' in sys.argv:
            _daily_comparison(Path(sys.argv[1]), patch)
        else:
            _production_comparison_preparation(Path(sys.argv[1]), {}, patch)
