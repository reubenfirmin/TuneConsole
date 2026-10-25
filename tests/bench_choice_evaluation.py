"""Opt-in #138 cost/coverage audit on a read-only backup of a real saved library.

uv run pytest -s tests/bench_choice_evaluation.py
This creates no live exposure, promotion or purported future outcomes. Every write
and recovery exercise is confined to pytest's disposable copy.
"""
import asyncio
import json
import logging
from pathlib import Path
import resource
import sys
import time

import pytest

from tests.bench_svd_background import _backup_library, _saved_library_source
from yt_playlist.core.numerics import limit_native_threads
from yt_playlist.core.store import Store
from yt_playlist.rec import album_choices, choice_evidence, model_selection as selection


def test_saved_library_production_comparison_preparation(tmp_path, monkeypatch):
    """Exercise production candidate and serving preparation on a read-only backup."""
    from yt_playlist.rec import comparison_preparation, model_setup, product_comparison
    backup = tmp_path / 'library.db'
    report = _backup_library(_saved_library_source(), backup)
    limit_native_threads()
    store = Store(backup)
    store.init_schema()
    try:
        # These resets affect only the disposable copy, never the source library.
        store.settings.set_setting('rec_selection_paused', '0')
        store.conn.execute("DELETE FROM rec_proposals WHERE surface='model_live_comparison'")
        store.conn.commit()
        previous = store.rec_model.get_rec_vectors()
        now, started = time.time(), time.monotonic()
        async def exercise():
            task = asyncio.create_task(asyncio.to_thread(selection.maintain, store, now))
            lag, reads = [], []
            while not task.done():
                due = time.monotonic() + .05
                await asyncio.sleep(.05)
                lag.append(max(0, time.monotonic() - due))
                read_at = time.monotonic()
                await asyncio.to_thread(store.rec_model.rec_vectors_count)
                reads.append(time.monotonic() - read_at)
            report.update(event_loop_samples=len(lag), event_loop_max_ms=max(lag, default=0) * 1000,
                          concurrent_read_samples=len(reads), concurrent_read_max_ms=max(reads, default=0) * 1000)
            return await task
        result = asyncio.run(exercise())
        report['wall_seconds'] = time.monotonic() - started
        report['result'] = result
        print(json.dumps({'preparation': result}), flush=True)
        assert result and result['status'] == 'running' and result['phase'] == 'observing', report
        assert previous == store.rec_model.get_rec_vectors()
        current = product_comparison.state(store)
        report['projected_bytes'] = sum(len(row[0]) for row in store.conn.execute(
            "SELECT payload FROM rec_proposals WHERE surface LIKE ?", (f"model_pair:{current['run_id']}:%",)))
        report['evidence_bytes'] = sum(path.stat().st_size for path in store.rec_ledger.evidence().directory.glob('*/*/*.json'))
        report['coverage'], pairs = {}, {}
        for arm in ('incumbent', 'challenger'):
            report['coverage'][arm], pairs[arm] = {}, {}
            for format in product_comparison.FORMATS:
                data = store.rec_proposals.get_proposals(product_comparison.pair_surface(current['run_id'], arm, format))['data']
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
        report['daily_refresh_timings'] = store.model_selection.selection_run(current['run_id'])['preparation_timings']
        destination = tmp_path / 'production-comparison.json'
        destination.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'report': str(destination), **report}), flush=True)
    finally:
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


def test_saved_library_choice_evaluation_cost_and_coverage(tmp_path, monkeypatch):
    backup = tmp_path / 'library.db'
    report = _backup_library(_saved_library_source(), backup)
    limit_native_threads()
    store = Store(backup)
    store.init_schema()
    now = time.time()
    original_fit = selection.training.build_vectors_from_baskets
    fits = []

    def fit(baskets, **kwargs):
        started, cpu = time.monotonic(), time.thread_time()
        keys, vectors = original_fit(baskets, **kwargs)
        fits.append({'method': kwargs.get('method'), 'dim': kwargs.get('dim'),
                     'baskets': len(baskets), 'tracks': len(keys),
                     'wall_s': time.monotonic() - started, 'cpu_s': time.thread_time() - cpu})
        return keys, vectors

    monkeypatch.setattr(selection.training, 'build_vectors_from_baskets', fit)
    try:
        report['readiness'] = selection.readiness(store, now)
        report['counts'] = {table: store.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                            for table in ('recordings', 'play_events', 'listening_observations',
                                          'rec_vectors', 'rec_model_forecasts')}
        report['precisions'] = dict(store.conn.execute(
            'SELECT precision,COUNT(*) FROM listening_observations GROUP BY precision'))
        report['exact_provenance'] = dict(store.conn.execute(
            'SELECT COALESCE(is_generated,0),COUNT(*) FROM play_events GROUP BY COALESCE(is_generated,0)'))
        windows, _ = selection._windows(store, now)
        report['historical_windows'] = [{
            'cutoff': window['cutoff'], 'end': window['end'], 'baskets': len(window['baskets']),
            'prior_keys': len(window['before']), 'new_organic_day_keys': len(window['held'])}
            for window in windows]
        existing = store.model_selection.selection_runs(limit=1)
        if existing:
            before_forecasts = len(store.model_selection.model_forecasts(existing[0]['id']))
            selection.update_followup(store, now)
            report['existing_followup'] = {
                'forecasts_before_audit': before_forecasts,
                'result': store.model_selection.selection_run(existing[0]['id']).get('followup')}
        before_vectors = store.rec_model.get_rec_vectors()
        before_setup = selection.current_setup(store)
        before_weights = store.rec_model.weight_records()

        async def exercise():
            started = time.monotonic()
            task = asyncio.create_task(asyncio.to_thread(selection.run, store, now))
            lag, reads = [], []
            while not task.done():
                due = time.monotonic() + .01
                await asyncio.sleep(.01)
                lag.append(max(0, time.monotonic() - due))
                read_at = time.monotonic()
                await asyncio.to_thread(store.rec_model.rec_vectors_count)
                reads.append(time.monotonic() - read_at)
            result = await task
            report.update(selection_wall_s=time.monotonic() - started,
                event_loop_samples=len(lag), event_loop_max_ms=max(lag, default=0) * 1000,
                concurrent_read_samples=len(reads), concurrent_read_max_ms=max(reads, default=0) * 1000)
            return result

        result = asyncio.run(exercise())
        assert result['status'] in {'retained', 'insufficient'}
        assert store.rec_model.get_rec_vectors() == before_vectors
        assert selection.current_setup(store) == before_setup
        assert store.rec_model.weight_records() == before_weights
        report['selection'] = result
        # Measure prospective computation without creating or observing invented outcomes.
        candidate = result.get('diagnostic_challenger') or {'method': 'svd', 'dim': 64}
        started = time.monotonic()
        forecast = selection._forecast(store, before_setup, candidate, now)
        report['paired_forecast'] = {'wall_s': time.monotonic() - started,
            'eligible': len(forecast['eligible']) if forecast else 0,
            'previous_top': len(forecast['previous']) if forecast else 0,
            'candidate_top': len(forecast['selected']) if forecast else 0,
            'later_outcomes': 'Not observed by this audit'}
        started = time.monotonic()
        albums = album_choices.prepare(store, now, store.discovery.get_discovered_albums())
        report['album_choices'] = {'wall_s': time.monotonic() - started, 'options': len(albums),
            'scored': sum(album['score'] is not None for album in albums)}
        started = time.monotonic()
        choice_evidence.daily(store, now)
        compiled = store.rec_proposals.get_proposals('listening_choices')
        report['daily_choices'] = {'wall_s': time.monotonic() - started,
            'captured_opportunities': compiled['source_opportunities'],
            'captured_responses': compiled['source_responses'],
            'note': 'The database backup does not import a live JSON choice ledger.'}
        # A crash after admission keeps the real serving vectors and recovers as failed.
        pending = store.model_selection.start_selection(now + 1, {'ran_at': now + 1,
            'status': 'running', 'previous': before_setup, 'selected': before_setup})
        store.conn.close()
        store = Store(backup)
        store.init_schema()
        selection.recover_interrupted(store)
        assert store.model_selection.selection_run(pending)['status'] == 'failed'
        assert store.rec_model.get_rec_vectors() == before_vectors
        report['restart_pending_recovered'] = True
        report['fits'] = fits
        report['database_bytes'] = backup.stat().st_size
        report['peak_process_rss_mib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if sys.platform == 'darwin' else 1024) / 1024 ** 2
        destination = tmp_path / 'choice-evaluation.json'
        destination.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'report': str(destination), **report}), flush=True)
    finally:
        store.conn.close()
