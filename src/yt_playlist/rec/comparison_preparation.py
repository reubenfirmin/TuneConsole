"""Worker-owned comparison preparation, decisions, publication and monitoring."""
import base64
from dataclasses import asdict
import hashlib
import json
import secrets
import time

from yt_playlist.core.activity import check_cancellable_work, work_budget, WorkBudgetExceeded, WorkClosing
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.rec import (album_choices, choice_evidence, discover,
    home_cards, home_prepared, measurement, mode_surfaces, model_setup, model_refresh,
    product_comparison, progressive, home_themes, home_vibe, taste_analysis, transient)

# Two complete serving preparations are substantially more work than one model
# fit. This infrequent private-reader job stays cancellable and has its own cap.
BUDGET_SECONDS = 90
MAX_PREPARED_BYTES = 12_000_000


def _rows(value):
    return [(key, base64.b64decode(vector)) for key, vector in value]


def _encode(rows):
    return [(key, base64.b64encode(vector).decode('ascii')) for key, vector in rows]


def _arm(reader, rows, setup, now, progress=None):
    get_vectors, get_setting = reader.rec_model.get_rec_vectors, reader.settings.get_setting
    get_proposals = reader.rec_proposals.get_proposals
    memo = getattr(reader, '_memoized_read', None)
    by_keys = reader.rec_model.vectors_by_keys
    try:
        return _calculate_arm(reader, rows, setup, now, progress or (lambda phase: None))
    finally:
        reader.rec_model.get_rec_vectors = get_vectors
        reader.settings.get_setting = get_setting
        reader.rec_proposals.get_proposals = get_proposals
        reader._memoized_read = memo
        reader.rec_model.vectors_by_keys = by_keys


def _calculate_arm(reader, rows, setup, now, progress):
    """Only this pinned reader sees the alternative. Serving SQLite is untouched."""
    memo = getattr(reader, '_memoized_read', None)
    identity = hashlib.sha256(b''.join(key.encode() + vector for key, vector in rows)).hexdigest()
    if memo:
        reader._memoized_read = lambda name, calculate: memo(('comparison', identity, name), calculate)
    get_setting = reader.settings.get_setting
    reader.settings.get_setting = lambda key: ({'rec_embed_method': setup['method'],
        'rec_dim': str(setup['dim'])}.get(key) if key in ('rec_embed_method', 'rec_dim') else get_setting(key))
    reader.rec_model.get_rec_vectors = lambda: rows
    row_lookup = dict(rows)
    original_by_keys = reader.rec_model.vectors_by_keys
    reader.rec_model.vectors_by_keys = lambda keys, content=False: (
        original_by_keys(keys, content=True) if content else {key: row_lookup[key] for key in keys if key in row_lookup})
    progress('context')
    context = choice_evidence.freeze_context(reader, choice_evidence.model_context(reader, now))
    if 'artifact' not in context:
        raise ValueError('The comparison context could not be retained')
    progress('ranked pools')
    policies = measurement.PoolPolicies(reader, now)
    bundles, fresh = mode_surfaces._calculate_bundles(reader, now, policies)
    # The same personal rankings order replenishment beyond the Home frontier.
    ranks = {}
    for pool in bundles.get('all', {}).values():
        for index, track in enumerate(pool):
            ranks[track['key']] = max(ranks.get(track['key'], 0), 1 / (index + 1))
    for keys in bundles.get('_radio', {}).get('members', {}).values():
        keys.sort(key=lambda key: (-ranks.get(key, 0), key))
    bundles.setdefault('_radio', {})['scores'] = ranks
    original = reader.rec_proposals.get_proposals
    reader.rec_proposals.get_proposals = lambda surface: (
        bundles if surface == 'mode_bundles' else fresh if surface == 'fresh_pool' else original(surface))
    plays = reader.rec_query.play_counts()
    facets = reader.rec_query.play_weighted_facets(plays)
    breadth = taste_analysis.taste_breadth(reader, genre_plays=facets['genres'])
    eras = taste_analysis.era_distribution(reader, era_plays=facets['eras'])
    selection_data = {'plays': plays,
        'preparation': {'leans': transient.facet_leans(reader, now),
                        'dists': (breadth['families'], dict(eras)), 'vibe_rank': home_vibe.ranker(reader, now)}}
    service = home_cards.HomeCards(reader, home_prepared._NoJobs(), home_prepared._LOG,
        genre_options=home_themes.genre_options(reader, genre_plays=facets['genres']), selection_data=selection_data)
    menus = []
    for offset in range(home_prepared.MAX_MENUS):
        progress(f'mix menu {offset + 1}')
        menu = asdict(service.cards(home_cards.CardsRequest(now, epoch=offset, record=False)))
        for proto in menu['protos']:
            proto['experiment_id'] = home_cards.experiment_id(proto)
            proto.setdefault('recipe', {})['experiment_id'] = proto['experiment_id']
        by_lane = {proto['lane']: proto for proto in menu['protos']}
        for slot in menu['slots']:
            if slot['proto']:
                slot['proto'] = by_lane[slot['proto']['lane']]
        menus.append(menu)
    progress('album frontier')
    albums = album_choices.prepare(reader, now, discover.pick_discovered_albums(reader, 60, now))
    radio_bundles = {key: value for key, value in bundles.items() if key != 'all'}
    if not bundles.get('_radio', {}).get('members'):
        from yt_playlist.rec.radio_candidates import CLUSTER_POOL_LIMIT
        radio_bundles['all'] = {lane: pool[:CLUSTER_POOL_LIMIT] for lane, pool in bundles.get('all', {}).items()}
    return {'_policies': list(policies.rows.values()),
            'mixes': {'menus': menus, 'choice_context': context, 'tuning': home_prepared.tuning_key(reader, now)},
            'albums': {'built_at': now, 'items': albums, 'context': context},
            'radio': {'bundles': radio_bundles, 'context': context,
                      'modes': [{**mode, 'centroid': mode['centroid'].tolist()} for mode in reader.modes.list_modes()],
                      'encoding_model': get_setting('rec_content_model')}}


def prepare(store, current, now, checkpoint=None):
    started = time.monotonic()
    refreshing = current.get('phase') == 'observing'
    timings, phase, phase_started = {}, 'candidate fit', started
    def progress(next_phase):
        nonlocal phase, phase_started
        check_cancellable_work()
        if checkpoint is not None:
            checkpoint()
        tick = time.monotonic()
        timings[phase] = tick - phase_started
        phase, phase_started = next_phase, tick
    try:
        with work_budget(BUDGET_SECONDS):
            # Both arms share one pinned input snapshot. Their overrides are
            # restored and memoized reads are isolated by frozen vector identity.
            with store.read_snapshot() as snapshot:
                reader = snapshot.store
                if refreshing:
                    artifact = current['vectors']
                    saved = reader.rec_ledger.evidence().get('artifact', artifact)
                    if not saved:
                        raise ValueError('The comparison vector snapshot is missing')
                    incumbent, challenger = _rows(saved['incumbent']), _rows(saved['challenger'])
                else:
                    incumbent = reader.rec_model.get_rec_vectors()
                    model_refresh._check_fit_memory(reader.rec_evidence.rec_baskets(), current['challenger']['dim'],
                                                   progressive.Budget().full_memory_bytes)
                    keys, matrix = model_setup.build(reader, current['challenger'])
                    if matrix is None or len(keys) < model_setup.MIN_TRACKS:
                        raise ValueError('Too few comparable modeled tracks to prepare a challenger')
                    challenger = list(zip(keys, [row.astype('float32').tobytes() for row in matrix]))
                    artifact = store.rec_ledger.evidence().artifact({'incumbent': _encode(incumbent),
                        'challenger': _encode(challenger)})
                # Read model and standing preferences as publication dependencies.
                if (model_setup.current_setup(reader) != current['previous']
                        or product_comparison.constraints(reader) != current['constraints']):
                    raise ValueError('Model or standing preferences changed during preparation')
                pairs, policy_rows = {}, []
                for arm, rows, setup in (('incumbent', incumbent, current['previous']),
                                          ('challenger', challenger, current['challenger'])):
                    pairs[arm] = _arm(reader, rows, setup, now, lambda name: progress(f'{arm}: {name}'))
                    policy_rows.extend(pairs[arm].pop('_policies'))
                progress('publication')
                if not refreshing:
                    current = {**current, 'started_at': now,
                        'exposure_until': now + current['policy']['horizon_days'] * product_comparison.DAY,
                        'decision_at': now + (current['policy']['horizon_days'] * 24
                            + current['policy']['followup_hours']) * 3600,
                        'vectors': artifact, 'generation': hashlib.sha256(canonical([artifact, current['policy']])).hexdigest()}
                current = {**current, 'phase': 'observing', 'prepared_at': now, 'last_prepare_attempt': now,
                           'runtime_seconds': time.monotonic() - started}
                encoded = {product_comparison.pair_surface(current['run_id'], arm, format):
                           json.dumps({'run_id': current['run_id'], 'data': data})
                       for arm, formats in pairs.items() for format, data in formats.items()}
                if sum(len(value.encode()) for value in encoded.values()) > MAX_PREPARED_BYTES:
                    raise ValueError('Paired serving projections exceeded their storage budget')
                run = reader.model_selection.selection_run(current['run_id'])
                run.pop('preparation_error', None)
                run.update(phase='observing', progress='Comparing complete menus and Radio episodes on observed listening.',
                           policy=current['policy'], decision_at=current['decision_at'],
                           runtime_seconds=current['runtime_seconds'], preparation_timings=timings)
                def publish():
                    store.rec_ledger.publish_prepared_policies(policy_rows)
                    for surface, value in encoded.items():
                        store.rec_proposals.publish_prepared_home(value, now, surface=surface)
                    store.rec_proposals.publish_prepared_home(json.dumps(current), now,
                                                             surface=product_comparison.SURFACE)
                    store.model_selection.publish_selection(current['run_id'], json.dumps(run), None, current['previous'])
                if not snapshot.publish(publish, allow_stale_tables=mode_surfaces.RANKING_CHURN_TABLES):
                    raise ValueError('Comparison inputs changed before publication')
                return True
    except WorkClosing:
        raise
    except Exception as exc:
        reason = (f'Comparison preparation exceeded its time budget during {phase}.' if isinstance(exc, WorkBudgetExceeded)
                  else f'Comparison preparation deferred: {exc}')
        run = store.model_selection.selection_run(current['run_id'])
        current['last_prepare_attempt'] = now
        if refreshing:
            run.update(preparation_error=reason, preparation_timings=timings)
        else:
            current.update(phase='failed', reason=reason, retry_at=now + product_comparison.DAY)
            run.update(status='failed', phase='complete', reason=reason, runtime_seconds=time.monotonic() - started,
                       preparation_timings=timings)
        store.model_selection.save_selection(current['run_id'], run)
        store.rec_proposals.put_proposals(product_comparison.SURFACE, current, now)
        return False


def promote(store, current, run, now):
    artifact = store.rec_ledger.evidence().get('artifact', current['vectors'])
    if not artifact:
        raise ValueError('The challenger vector snapshot is missing; current model kept')
    rows = _rows(artifact['challenger'])
    run.update(selected=current['challenger'], serving_vectors=current['vectors'])
    monitor = {'run_id': current['run_id'], 'generation': current['vectors'], 'setup': current['challenger'],
        'started_at': now, 'exposure_until': now + 28 * product_comparison.DAY,
        'next_at': now + 8 * product_comparison.DAY, 'policy': current['policy'],
        'baseline': current['report']['overall']['samples']['challenger']}
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        if (product_comparison.constraints(reader) != current['constraints']
                or model_setup.current_setup(reader) != current['previous']):
            raise ValueError('Model or standing preferences changed before promotion')
        # Invalidate derived projections in the same transaction as the new
        # model. Existing worker preparation republishes coherent menus.
        def publish():
            store.model_selection.publish_selection(current['run_id'], json.dumps(run), rows, current['challenger'])
            store.rec_proposals.publish_prepared_home(json.dumps({key: value for key, value in current.items() if key != 'report'}),
                                                     now, surface=product_comparison.SURFACE)
            store.rec_proposals.publish_prepared_home(json.dumps(current['report']), now, surface='model_live_report')
            store.rec_proposals.publish_prepared_home(json.dumps(monitor), now, surface=product_comparison.MONITOR)
            store.conn.execute("DELETE FROM rec_proposals WHERE surface IN "
                "('home_prepared','home_tuned','home_choice','album_prepared','mode_bundles','fresh_pool')")
        if not snapshot.publish(publish):
            raise ValueError('Inputs changed before promotion; current model kept')


def maintain(store, now, *, checkpoint=None):
    """One bounded preparation or daily decision step on the existing worker."""
    monitor(store, now)
    current = product_comparison.state(store)
    paused = store.settings.get_setting('rec_selection_paused') == '1'
    if current.get('phase') == 'observing':
        if (paused or model_setup.current_setup(store) != current['previous']
                or product_comparison.constraints(store) != current['constraints']):
            _finish(store, current, 'retained', 'Standing preferences changed; ending this comparison epoch.', now)
            return
        report = product_comparison.measure(store, current, now)
        status, reason = product_comparison.decision(current, report, now)
        current['report'] = report
        if status != 'observing':
            _finish(store, current, status, reason, now)
        else:
            store.rec_proposals.put_proposals('model_live_report', report, now)
            store.rec_proposals.put_proposals(product_comparison.SURFACE,
                {key: value for key, value in current.items() if key != 'report'}, now)
            if (now < current['exposure_until']
                    and now - current.get('last_prepare_attempt', now) >= product_comparison.DAY):
                prepare(store, {key: value for key, value in current.items() if key != 'report'}, now, checkpoint)
        return
    if paused or current.get('retry_at', 0) > now:
        return
    if store.rec_model.rec_vectors_count() < model_setup.MIN_TRACKS:
        return
    bounds = store.charts.history_bounds()
    if (bounds[0] is None or bounds[1] is None
            or (min(bounds[1], now) - bounds[0]) / product_comparison.DAY < model_setup.MIN_HISTORY_DAYS):
        return
    if current.get('phase') not in ('preparing',):
        previous = model_setup.current_setup(store)
        # Restore clears disposable serving state, not the programme's error
        # budget. Retained run summaries survive recovery and storage cleanup.
        with store._lock:
            attempts = max(current.get('attempt', 0), store.conn.execute(
                "SELECT COUNT(*) FROM rec_model_selection_runs WHERE json_extract(result_json,'$.kind')='product'").fetchone()[0]) + 1
        alternatives = [dict(method=method, dim=dim) for method, dim in model_setup.CONFIGS
                        if (method, dim) != (previous['method'], previous['dim'])]
        if not alternatives:
            return
        challenger = alternatives[(attempts - 1) % len(alternatives)]
        policy = dict(product_comparison.POLICY)
        current = {'phase': 'preparing', 'attempt': attempts, 'seed': secrets.token_hex(16),
            'policy': policy, 'alpha': policy['family_alpha'] / (attempts * (attempts + 1)),
            'previous': previous, 'challenger': challenger, 'constraints': product_comparison.constraints(store)}
        result = {'kind': 'product', 'phase': 'preparing', 'ran_at': now, 'status': 'running',
            'previous': previous, 'selected': previous, 'challenger': challenger, 'grid': [],
            'progress': 'Preparing one controlled listening comparison. Current model remains available.'}
        current['run_id'] = store.model_selection.start_selection(now, result)
        store.settings.set_setting('rec_selection_live_run', str(current['run_id']))
        store.rec_proposals.put_proposals(product_comparison.SURFACE, current, now)
    prepare(store, current, now, checkpoint)
    return store.model_selection.selection_run(current['run_id'])


def _finish(store, current, status, reason, now):
    run = store.model_selection.selection_run(current['run_id'])
    run.update(status=status, phase='complete', reason=reason, product_report=current.get('report'),
               decision_at=now, policy=current['policy'])
    current.update(phase=status, reason=reason,
                   retry_at=now + current['policy']['retry_days'] * product_comparison.DAY)
    if status == 'applied':
        try:
            current['retry_at'] = now + (28 + current['policy']['retry_days']) * product_comparison.DAY
            promote(store, current, run, now)
            return
        except ValueError as exc:
            status = 'retained'
            reason = str(exc)
            run.update(status=status, reason=reason, selected=current['previous'])
    current.update(phase=status, reason=reason,
                   retry_at=now + current['policy']['retry_days'] * product_comparison.DAY)
    with store._lock, store.conn:
        store.conn.execute('UPDATE rec_model_selection_runs SET result_json=result_json WHERE id=?', (current['run_id'],))
        store.model_selection.publish_selection(current['run_id'], json.dumps(run), None, current['previous'])
        store.rec_proposals.publish_prepared_home(json.dumps({key: value for key, value in current.items() if key != 'report'}),
                                                 now, surface=product_comparison.SURFACE)
        store.rec_proposals.publish_prepared_home(json.dumps(current.get('report') or {}), now, surface='model_live_report')


def monitor(store, now):
    """Inspect nonoverlapping weeks after promotion; offer review, never infer causality."""
    plan = store.rec_proposals.get_proposals(product_comparison.MONITOR) or {}
    if not plan or now < plan.get('next_at', float('inf')) or plan.get('complete'):
        return
    if store.settings.get_setting('rec_selection_paused') == '1':
        return
    cutoff = min(plan['started_at'] + 7 * product_comparison.DAY, plan['exposure_until'])
    cohort = {**plan, 'exposure_until': cutoff, 'mode': 'monitor', 'alpha': .01}
    report = product_comparison.measure(store, cohort, now)
    baseline = plan['baseline']
    samples = report['overall']['samples']['challenger']
    interval = product_comparison._interval(baseline, samples, .01)
    mature = sum(row['arms']['challenger'].get('mature', 0) for row in report['formats'].values())
    observed = sum(row['arms']['challenger'].get('observed', 0) for row in report['formats'].values())
    enough = len(samples) >= 4 and observed >= 10 and observed >= .7 * max(1, mature)
    decline = bool(enough and interval['interval'] and interval['interval'][1] < 0 and interval['difference'] < -.1)
    plan['declining_weeks'] = plan.get('declining_weeks', 0) + 1 if decline else 0
    report.update(comparison=interval, status='review' if plan['declining_weeks'] >= 2 else 'observing' if enough else 'insufficient',
        explanation='Later listening is observational. Two successive observed weeks with a supported decline prompt review; restoring stays your choice.')
    run = store.model_selection.selection_run(plan['run_id'])
    if run:
        run['monitoring'] = report
        run.setdefault('monitoring_weeks', []).append(report)
        store.model_selection.save_selection(plan['run_id'], run)
    plan.update(started_at=cutoff, next_at=cutoff + 8 * product_comparison.DAY,
                complete=cutoff >= plan['exposure_until'])
    store.rec_proposals.put_proposals(product_comparison.MONITOR, plan, now)
