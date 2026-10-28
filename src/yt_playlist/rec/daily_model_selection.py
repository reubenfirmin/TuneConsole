"""Daily model comparisons with all prior history and one 30-day holdout.

Historical fits share detached, conservatively reconstructed inputs. One fit is
admitted per worker step. Completed predictions survive a restart; no reader or
WAL snapshot remains open while fitting. Serving changes only after a complete
comparison and a separately guarded fit on current inputs.
"""
import hashlib
import json
import math

import numpy as np

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_stopping, work_budget
from yt_playlist.core.evidence_ledger import canonical
from yt_playlist.rec import embedding_training as training, model_refresh, model_setup, progressive
from yt_playlist.rec.vector_math import rank_by_context
from yt_playlist.repos.rec_model import VectorChanges

DAY = 86400
HOLDOUT_DAYS = 30
MIN_TRIALS = 50
MIN_ACTIVE_DAYS = 7
MIN_FRESH_TRACKS = 30
STEP_SECONDS = 20
SCHEDULE = 'daily_model_schedule'
POLICY = 'all-prior-30-day-holdout-v2'


def due(store, now):
    if (store.settings.get_setting('rec_selection_paused') == '1'
            or store.settings.get_setting('rec_selection_restore')):
        return False
    return now >= (store.rec_proposals.get_proposals(SCHEDULE) or {}).get('next_at', 0)


def _schedule(store, now, delay):
    store.rec_proposals.put_proposals(SCHEDULE, {'next_at': now + delay}, now)


def readiness(store, now):
    with store.read_view() as reader:
        return _readiness(reader, now)


def _readiness(reader, now):
    lo, hi = reader.rec_evidence.evaluation_history_bounds()
    days = max(0, (min(hi, now) - lo) / DAY) if lo is not None and hi is not None else 0
    minimum_days = HOLDOUT_DAYS + model_setup.MIN_HISTORY_DAYS
    tracks = reader.rec_model.rec_vectors_count()
    checks = [dict(label='Listening history', value=min(int(days), minimum_days),
                   total=minimum_days, unit='days'),
              dict(label='Modeled tracks', value=min(tracks, model_setup.MIN_TRACKS),
                   total=model_setup.MIN_TRACKS, unit='tracks')]
    if reader.settings.get_setting('rec_selection_restore'):
        return dict(state='restoring', reason='Restoring the previous model.', checks=checks)
    if reader.settings.get_setting('rec_selection_paused') == '1':
        return dict(state='paused', reason='Automatic selection is paused. Regular training continues.', checks=checks)
    last = reader.model_selection.latest_daily_selection()
    if last and last['status'] == 'running':
        return dict(state='running', reason=last.get('progress', 'Comparing model setups.'), checks=checks)
    if days < minimum_days or tracks < model_setup.MIN_TRACKS:
        return dict(state='waiting', reason='Need 14 days of training history before the 30-day comparison period, '
                    'and 100 modeled tracks.', checks=checks)
    if last and now < last['ran_at'] + DAY:
        return dict(state='waiting', reason='The next daily comparison is scheduled.',
                    next_at=last['ran_at'] + DAY, checks=checks)
    # An insufficient attempt has not completed a comparison. Let newly
    # sufficient holdout activity admit the first fit after the daily delay.
    if last and last['status'] not in ('failed', 'insufficient'):
        fresh = reader.model_selection.comparison_activity(last['evidence_end'], now)
        checks.append(dict(label='Fresh listening since the last comparison', value=min(len(fresh), MIN_FRESH_TRACKS),
                           total=MIN_FRESH_TRACKS, unit='distinct tracks'))
        if len(fresh) < MIN_FRESH_TRACKS:
            return dict(state='waiting', reason='Waiting for 30 distinct freshly played tracks before another comparison.',
                        checks=checks)
    return dict(state='ready', reason='Ready for the next daily comparison.', checks=checks)


def _capture(reader, end):
    cutoff = end - HOLDOUT_DAYS * DAY
    # Playlist timestamps are inclusive in the reconstruction API; the split
    # here is strictly before the first held-out timestamp.
    baskets, coverage = reader.rec_evidence.rec_baskets_as_of(math.nextafter(cutoff, -math.inf))
    baskets = training.canonical_baskets(baskets)
    keys = {key for basket in baskets for key in basket}
    before = reader.history.history_keys_before(cutoff)
    activity = reader.model_selection.comparison_activity(cutoff, end)
    generated = reader.rec_evidence.generated_only_keys_between(cutoff, end)
    eligible = keys - before
    held = {key: int(at // DAY) for key, at in activity.items()
            if key in eligible and key not in generated}
    weights = (training.cooc_weights(reader.history.play_counts_before(cutoff))
               if reader.settings.get_setting('rec_cooc_weighting') == '1' else None)
    captured = dict(baskets=baskets, context=sorted(before & keys), eligible=sorted(eligible),
                    held=held, weights=weights, cutoff=cutoff, end=end, coverage=coverage,
                    generated_excluded=len(eligible & generated),
                    unavailable_targets=len(set(activity) - keys - before))
    return captured, hashlib.sha256(canonical([POLICY, captured])).hexdigest()


def _evaluate(setup, captured):
    vocabulary = set(captured['eligible']) | set(captured['context'])
    if len(vocabulary) < setup['dim'] + training.MIN_VOCAB_MARGIN:
        return {**setup, 'valid': False, 'unsupported': True,
                'reason': f"Needs at least {setup['dim'] + training.MIN_VOCAB_MARGIN} reconstructable tracks."}
    model_refresh._check_fit_memory(captured['baskets'], setup['dim'], progressive.Budget().full_memory_bytes)
    keys, vectors = training.build_vectors_from_baskets(captured['baskets'], dim=setup['dim'],
                                                       method=setup['method'], weights=captured['weights'])
    check_stopping()
    if not keys or set(keys) != vocabulary or not np.isfinite(vectors).all():
        return {**setup, 'valid': False, 'reason': 'The setup could not model the complete comparison universe.'}
    predictions = rank_by_context(keys, vectors, set(captured['context']), set(captured['eligible']))
    return {**setup, 'valid': True, 'predictions': sorted(predictions), 'vectors': len(keys)}


def _compare(baseline, candidate, captured, alpha):
    held = captured['held']
    previous, selected = set(baseline['predictions']), set(candidate['predictions'])
    old_hits, new_hits = len(held.keys() & previous), len(held.keys() & selected)
    by_day = {}
    for key, day in held.items():
        by_day[day] = by_day.get(day, 0) + int(key in selected) - int(key in previous)
    wins = sum(delta > 0 for delta in by_day.values())
    losses = sum(delta < 0 for delta in by_day.values())
    discordant = wins + losses
    # One vote per active UTC day, rather than treating a listening binge as
    # many independent successes. This sign test assumes comparable day units;
    # it does not establish causality or eliminate changes in listening habits.
    p = sum(math.comb(discordant, j) for j in range(wins, discordant + 1)) / 2 ** discordant if discordant else 1.
    n = len(held)
    gain = (new_hits - old_hits) / n if n else 0
    enough = n >= MIN_TRIALS and len(by_day) >= MIN_ACTIVE_DAYS
    promote = enough and new_hits - old_hits >= 5 and gain >= .02 and p <= alpha
    return dict(eligible=enough, promote=promote, trials=n, windows=1, active_days=len(by_day),
                previous_hits=old_hits, selected_hits=new_hits, previous_score=old_hits / n if n else None,
                score=new_hits / n if n else None, p=p, alpha=alpha, confidence_scope='comparison',
                reason='Clear prediction improvement' if promote else 'No supported prediction improvement')


def _retire_product_trial(store, now):
    """End new legacy assignments while keeping held projections and evidence."""
    from yt_playlist.rec import product_comparison
    current = product_comparison.state(store)
    if current.get('phase') not in ('preparing', 'observing'):
        return
    reason = 'Replaced by daily prediction comparisons with a 30-day holdout.'
    with store._lock, store.conn:
        current = product_comparison.state(store)
        if current.get('phase') not in ('preparing', 'observing'):
            return
        run = store.model_selection.selection_run(current['run_id'])
        if run:
            run.update(status='retained', phase='complete', reason=reason, ended_at=now)
            store.conn.execute('UPDATE rec_model_selection_runs SET result_json=result_json WHERE id=?', (run['id'],))
            store.model_selection.publish_selection(run['id'], json.dumps(run), None, run['previous'])
        current.update(phase='retained', reason=reason, ended_at=now)
        store.rec_proposals.publish_prepared_home(json.dumps(current), now, surface=product_comparison.SURFACE)
        store.settings.publish_values({'rec_selection_live_run': ''})
        monitor = store.rec_proposals.get_proposals(product_comparison.MONITOR)
        if monitor:
            store.rec_proposals.publish_prepared_home(json.dumps({**monitor, 'complete': True}),
                                                     now, surface=product_comparison.MONITOR)


def _finish(store, run, now, *, status, reason):
    run.update(status=status, phase='complete', reason=reason, completed_at=now)
    run.pop('_evaluations', None)
    store.model_selection.save_selection(run['id'], run)
    _schedule(store, now, max(0, run['ran_at'] + DAY - now))
    return run


def _publish_fit(store, snapshot, run, now):
    reader = snapshot.store
    selected = run['selected']
    # This reader also checks the historical evidence again; corrected imports
    # must invalidate the saved comparison before a model can be published.
    baskets = training.canonical_baskets(reader.rec_evidence.rec_baskets())
    weights = (training.cooc_weights(reader.rec_query.play_counts())
               if reader.settings.get_setting('rec_cooc_weighting') == '1' else None)
    previous_rows = dict(reader.rec_model.get_rec_vectors())
    snapshot.release_reader()
    model_refresh._check_fit_memory(baskets, selected['dim'], progressive.Budget().full_memory_bytes)
    keys, vectors = training.build_vectors_from_baskets(baskets, dim=selected['dim'],
                                                       method=selected['method'], weights=weights)
    check_stopping()
    if len(keys) < model_setup.MIN_TRACKS or not np.isfinite(vectors).all():
        raise ValueError('The selected setup could not fit the current library')
    rows = [(key, vector.astype('float32').tobytes()) for key, vector in zip(keys, vectors)]
    fresh = dict(rows)
    changes = {'rec_vectors': VectorChanges(tuple((key, blob) for key, blob in rows
                if previous_rows.get(key) != blob), tuple(previous_rows.keys() - fresh.keys()))}
    fit_inputs = model_refresh._fingerprint(['canonical-baskets-1', selected['method'], selected['dim'], baskets, weights])
    applied = selected != run['previous']
    result = {key: value for key, value in run.items() if key != '_evaluations'}
    result.update(status='applied' if applied else 'retained', phase='complete', completed_at=now,
                  published_vectors=True, reason='Selected a setup with better held-out predictions.' if applied
                  else 'No alternative showed a supported improvement. The current setup was retrained.')
    encoded = json.dumps(result)
    def publish():
        store.model_selection.publish_selection(run['id'], encoded, rows if applied else None, selected)
        if not applied:
            store.rec_model.publish_training_generation(changes)
        store.settings.publish_values({'rec_fit_inputs': fit_inputs})
        store.conn.execute("DELETE FROM rec_proposals WHERE surface IN "
            "('home_prepared','home_tuned','home_choice','album_prepared','mode_bundles','fresh_pool')")
        store.rec_proposals.publish_prepared_home(json.dumps({'next_at': run['ran_at'] + DAY}), now, surface=SCHEDULE)
    if not snapshot.publish(publish):
        raise ValueError('Model inputs changed before publication; the serving model was kept')
    return result


def maintain(store, now, *, checkpoint=None):
    if not due(store, now):
        return None
    _retire_product_trial(store, now)
    run = store.model_selection.latest_daily_selection()
    if not run or run['status'] != 'running':
        state = readiness(store, now)
        if state['state'] != 'ready':
            _schedule(store, now, max(60, state.get('next_at', now + 3600) - now))
            return None
        previous = model_setup.current_setup(store)
        run = dict(kind='daily', policy=POLICY, status='running', phase='comparison', ran_at=now,
                   evidence_end=now, previous=previous, selected=previous, grid=[], _evaluations=[],
                   holdout_days=HOLDOUT_DAYS, progress='Preparing all prior history and the latest 30-day holdout.')
        run['id'] = store.model_selection.start_selection(now, run)
    elif now - run['ran_at'] > DAY:
        return _finish(store, run, now, status='failed', reason='The comparison expired before completion; current model kept.')
    try:
        with work_budget(STEP_SECONDS), store.read_snapshot() as snapshot:
            reader = snapshot.store
            if model_setup.current_setup(reader) != run['previous']:
                return _finish(store, run, now, status='retained', reason='The serving setup changed during comparison.')
            if (reader.settings.get_setting('rec_selection_paused') == '1'
                    or reader.settings.get_setting('rec_selection_restore')):
                return _finish(store, run, now, status='retained', reason='Automatic selection was paused.')
            captured, fingerprint = _capture(reader, run['evidence_end'])
            run['training_cutoff'] = captured['cutoff']
            run['coverage'] = captured['coverage']
            run['exclusions'] = {name: captured[name] for name in ('generated_excluded', 'unavailable_targets')}
            n, days = len(captured['held']), len(set(captured['held'].values()))
            run['baseline'] = dict(trials=n, windows=1, active_days=days)
            if n < MIN_TRIALS or days < MIN_ACTIVE_DAYS or not captured['context'] or len(captured['eligible']) <= 20:
                return _finish(store, run, now, status='insufficient',
                    reason='Need 50 comparable first-play tracks across seven active days in the latest 30 days.')
            setups = [run['previous']] + [dict(method=method, dim=dim) for method, dim in model_setup.CONFIGS
                if (method, dim) != (run['previous']['method'], run['previous']['dim'])]
            if run.get('input_fingerprint') != fingerprint or run.get('setups') != setups:
                run.update(policy=POLICY, input_fingerprint=fingerprint, setups=setups,
                           _evaluations=[], phase='comparison')
            evaluations = run['_evaluations']
            if len(evaluations) < len(setups):
                setup = setups[len(evaluations)]
                snapshot.release_reader()
                measured = _evaluate(setup, captured)
                evaluations.append(measured)
                run['budget_retries'] = 0
                run['progress'] = f'Compared {len(evaluations)} of {len(setups)} setups against the 30-day holdout.'
                store.model_selection.save_selection(run['id'], run)
                _schedule(store, now, 5)
                if checkpoint is not None:
                    checkpoint()
                return run
            if not evaluations[0]['valid']:
                return _finish(store, run, now, status='insufficient',
                    reason='The current setup could not be reconstructed before the holdout; current model kept.')
            if any(not value['valid'] and not value.get('unsupported') for value in evaluations[1:]):
                run['grid'] = evaluations
                return _finish(store, run, now, status='failed',
                    reason='A setup could not complete the shared comparison; current model kept.')
            # Correct the alternatives within this comparison. A shrinking
            # lifetime budget eventually makes a finite 30-day test unable to
            # promote any setup, irrespective of its predictions. This is a
            # per-comparison guard, not a lifetime false-promotion guarantee;
            # overlapping holdouts and the day-independence assumption remain.
            alpha = .05 / max(1, len(setups) - 1)
            baseline = evaluations[0]
            run['grid'] = [{**value, **_compare(baseline, value, captured, alpha)} if value['valid']
                           else {**value, 'eligible': False} for value in evaluations[1:]]
            winners = [value for value in run['grid'] if value.get('promote')]
            best = max(winners, key=lambda value: value['score']) if winners else None
            # Equal top scores retain the incumbent rather than choosing by order.
            if best and sum(value['score'] == best['score'] for value in winners) > 1:
                best = None
            run['selected'] = {key: best[key] for key in ('method', 'dim')} if best else run['previous']
            run['comparison'] = _compare(baseline, best or baseline, captured, alpha)
            del captured
            return _publish_fit(store, snapshot, run, now)
    except WorkClosing:
        raise
    except WorkBudgetExceeded:
        durable = store.model_selection.selection_run(run['id'])
        if durable and durable.get('phase') == 'complete':
            return durable  # The deadline may expire after an atomic commit.
        run['budget_retries'] = run.get('budget_retries', 0) + 1
        if run['budget_retries'] >= 3:
            return _finish(store, run, now, status='failed',
                reason='A fitting step exceeded its time budget three times; current model kept.')
        run['progress'] = 'A fitting step reached its time budget. Completed comparisons are saved for retry.'
        store.model_selection.save_selection(run['id'], run)
        _schedule(store, now, 60)
        return run
    except (ValueError, np.linalg.LinAlgError, model_refresh.MemoryBudgetExceeded) as exc:
        run['selected'] = run['previous']
        return _finish(store, run, now, status='failed', reason=f'Comparison deferred; current model kept: {exc}')
