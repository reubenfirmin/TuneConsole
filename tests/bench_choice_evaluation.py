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

from tests.bench_svd_background import _saved_library_source
from tests.library_image_fixtures import benchmark_library
from yt_playlist.core.numerics import limit_native_threads
from yt_playlist.core.store import Store
from yt_playlist.rec import (album_choices, choice_evidence, daily_model_selection,
                            model_selection as selection)


def test_saved_library_album_menu_frontier(tmp_path):
    """Audit the revised album policy on a disposable copy, without live exposure."""
    from yt_playlist.rec import album_tuning
    backup = tmp_path / 'library.db'
    backup, report = benchmark_library(_saved_library_source(), backup)
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
    backup, report = benchmark_library(_saved_library_source(), backup)
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
    backup, report = benchmark_library(_saved_library_source(), backup)
    report['source'] = str(_saved_library_source())
    command = [sys.executable, '-m', 'tests.bench_choice_evaluation', str(backup)]
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
        _daily_comparison(Path(sys.argv[1]), patch)
