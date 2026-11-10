"""Opt-in retained-menu audit on a disposable library backup.

Run with uv run pytest -s tests/bench_choice_retention.py. TUNECONSOLE_BENCH_DB
may select an existing isolated actual-library export to avoid a full live backup.
Current model comparison and restore use the daily-selection audit in
tests/bench_choice_evaluation.py; this check covers Home retention independently.
"""
import json
from pathlib import Path
import time

from tests.bench_svd_background import _saved_library_source
from tests.library_image.fixtures import benchmark_library
from yt_playlist.core.activity import work_budget
from yt_playlist.core.store import Store
from yt_playlist.rec import (choice_evidence as choices, candidate_refresh,
                            home_prepared, group_serving, rec_params)


def inventory(ledger):
    return {kind: {'records': len(paths), 'bytes': sum(path.stat().st_size for path in paths)}
            for kind in ('opportunity', 'response', 'artifact', 'checkpoint', 'report')
            for paths in [list((ledger.directory / kind).glob('*/*.json'))]}


def test_actual_library_report_refresh_and_unanswered_retirement_keep_full_menus(tmp_path):
    source, path = _saved_library_source(), tmp_path / 'library.db'
    path, backup = benchmark_library(source, path)
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
            old = now - (rec_params.get_param(store, 'choice_retention_d') + 1) * 86400
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
                          'backup': backup,
                          'unchanged_generation': generation, 'pools_unchanged': True,
                          'candidate_preferences_unchanged': True,
                          'controlled_clock': now, 'source': str(source)}), flush=True)
    finally:
        store.conn.close()
