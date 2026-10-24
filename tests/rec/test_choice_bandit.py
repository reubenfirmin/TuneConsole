import sqlite3

import numpy as np
import pytest

from yt_playlist.rec import choice_bandit, choice_evidence as choices, mode_eval, rec_params


def modes(store, *, space='basis', flipped=False, reps=('a', 'b')):
    store.modes.replace_modes([{'mode_id': i + 1, 'label': f'Group {i + 1}',
        'families': [['Jazz', 2]], 'centroid': np.eye(2)[1 - i if flipped else i],
        'size': 2, 'rep_keys': list(reps), 'space': space} for i in range(2)], [], 1)


def menu(store, oid='menu', now=100, reverse=False):
    options = [{'id': 'one', 'mode_id': 1}, {'id': 'two', 'mode_id': 2}]
    if reverse:
        options.reverse()
    for index, option in enumerate(options):
        option['position'] = index + 1
    groups = [{**mode, 'centroid': mode['centroid'].tolist()}
              for mode in store.modes.list_modes(active_only=True)]
    return choices.issue(store, 'mixes', options, now, context={'at': now, 'groups': groups},
                         opportunity_id=oid)


def evidence(store, now):
    report = choices.compile_report(store, now)
    return report['bandit'], {row['mode_id']: row for row in report['bandit']['rows']}


def test_missing_outcomes_supply_no_failures_and_delayed_observations_update_same_unit(store):
    modes(store)
    oid = menu(store)
    choices.respond(store, oid, 'visible', 101, visible=['one', 'two'])
    assert not evidence(store, 102)[1]
    choices.respond(store, oid, 'choice', 103, option_id='one')
    first, rows = evidence(store, 104)
    assert rows[1]['positive'] == rows[2]['negative'] == .25
    assert rows[1]['negative'] == rows[2]['positive'] == 0
    choices.respond(store, oid, 'start', 110, option_id='one')
    assert evidence(store, 109)[1][1]['positive'] == .25
    state, rows = evidence(store, 111)
    assert rows[1]['positive'] == .5 and rows[1]['days'] == 1
    assert 0 <= rows[1]['low'] < rows[1]['high'] <= 1
    assert mode_eval.mode_bandit_stats(store, now=112)[1] == (.5, .5)
    # A changed qualification policy or future/stale state never revives old raw offers.
    rec_params.set_param(store, 'bandit_reward_start', 0)
    assert mode_eval.mode_bandit_stats(store, now=112) == {}
    assert mode_eval.mode_bandit_stats(store, now=99) == {}


def test_episode_length_rerenders_and_display_position_do_not_multiply_response_mass(store):
    modes(store)
    for index in range(3):
        oid = menu(store, oid=f'menu-{index}', now=100 + index * 20, reverse=bool(index % 2))
        choices.respond(store, oid, 'visible', 101 + index * 20, visible=['one', 'two'])
        choices.respond(store, oid, 'choice', 102 + index * 20, option_id='one')
        choices.respond(store, oid, 'start', 103 + index * 20, option_id='one')
        for track in range(24):
            choices.respond(store, oid, 'experience', 104 + index * 20 + track, option_id='one',
                action_id=f'{index}:exit:{track}', details={'kind': 'track_exit', 'position': 190,
                                                          'duration': 200, 'video_id': str(track)})
    state, rows = evidence(store, 200)
    assert rows[1]['positive'] == 1 and rows[1]['days'] == 1
    assert rows[2]['negative'] == .25 and rows[2]['days'] == 1
    # Rendering an unobserved opportunity changes coverage, not effective allocation identity.
    menu(store, oid='hidden', now=201)
    assert evidence(store, 202)[0]['state_version'] == state['state_version']
    choices.respond(store, 'menu-0', 'promote', 203, option_id='one')
    assert evidence(store, 204)[1][1]['positive'] == 1  # Still one group-day unit.


def test_old_cohorts_expire_and_mode_drift_retires_incompatible_evidence(store):
    modes(store)
    oid = menu(store)
    choices.respond(store, oid, 'visible', 101, visible=['one', 'two'])
    choices.respond(store, oid, 'choice', 102, option_id='one')
    state, rows = evidence(store, 103)
    assert len(rows) == 2
    assert choice_bandit.stats(store, state['expires_at']) == {}
    assert not evidence(store, state['expires_at'])[1]
    modes(store, flipped=True)
    assert choice_bandit.stats(store, 104) == {}
    state, rows = evidence(store, 104)
    assert not rows and state['excluded']['drifted_mode'] == 2
    # Across a basis rebuild, representative identity overlap is the conservative fallback.
    modes(store, space='rebuilt', reps=('a', 'c'))
    assert len(evidence(store, 105)[1]) == 2
    modes(store, space='different', reps=('x', 'y'))
    assert not evidence(store, 106)[1]


def test_later_choices_from_held_menu_keep_days_and_do_not_reuse_old_completion(store):
    modes(store)
    oid = menu(store)
    choices.respond(store, oid, 'visible', 101, visible=['one', 'two'])
    choices.respond(store, oid, 'choice', 102, option_id='one', action_id='first')
    choices.respond(store, oid, 'start', 103, option_id='one')
    choices.respond(store, oid, 'experience', 104, option_id='one', details={
        'kind': 'track_exit', 'position': 190, 'duration': 200})
    choices.respond(store, oid, 'choice', 86400 + 102, option_id='two', action_id='second')
    # A network retry is the same decision; the second deliberate choice is retained.
    choices.respond(store, oid, 'choice', 86400 + 102, option_id='two', action_id='second')
    state, rows = evidence(store, 86400 + 110)
    assert rows[1]['positive'] == 1 and rows[1]['negative'] == .25
    assert rows[2]['positive'] == rows[2]['negative'] == .25
    assert rows[1]['days'] == rows[2]['days'] == 2
    choices.respond(store, oid, 'choice', 2 * 86400 + 102, option_id='one', action_id='third')
    state, rows = evidence(store, 2 * 86400 + 110)
    assert rows[1]['positive'] == 1.25  # Earlier completion is not copied to the new day.
    assert rows[1]['days'] == rows[2]['days'] == 3


def test_findings_publish_atomically_and_corrections_recompute_support(store):
    modes(store)
    oid = menu(store)
    choices.respond(store, oid, 'visible', 101, visible=['one', 'two'])
    choices.respond(store, oid, 'choice', 102, option_id='one')
    old = choices.compile_report(store, 103)
    choices.respond(store, oid, 'withdraw', 104, details={'action_id': 'menu:choice:one'})
    store.conn.execute("CREATE TEMP TRIGGER fail_choice_publication BEFORE UPDATE ON rec_proposals "
                       "WHEN NEW.surface='choice_interests' BEGIN SELECT RAISE(ABORT,'interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match='interrupted'):
        choices.compile_report(store, 105)
    assert store.rec_proposals.get_proposals('listening_choices')['watermark'] == old['watermark']
    assert store.rec_proposals.get_proposals('choice_bandit') == old['bandit']
    store.conn.execute('DROP TRIGGER fail_choice_publication')
    assert not evidence(store, 106)[1]


def test_shared_context_components_preserve_mode_reconciliation_and_missing_data_abstains(store):
    modes(store)
    groups = [{**mode, 'centroid': mode['centroid'].tolist(), 'detail': 'x' * 40000}
              for mode in store.modes.list_modes(active_only=True)]
    context = choices.freeze_context(store, {'at': 100, 'groups': groups})
    ledger = store.rec_ledger.evidence()
    manifest = ledger.get('artifact', context['artifact'])
    assert set(manifest['groups']) == {'$artifact'}
    oid = choices.issue(store, 'mixes', [{'id': 'one', 'mode_id': 1}, {'id': 'two', 'mode_id': 2}],
                        100, context=context, opportunity_id='shared-context')
    choices.respond(store, oid, 'visible', 101, visible=['one', 'two'])
    choices.respond(store, oid, 'choice', 102, option_id='one')
    state, rows = evidence(store, 103)
    assert rows[1]['positive'] == rows[2]['negative'] == .25
    assert state['excluded']['missing_context'] == 0
    # A retained offer with a missing component is unknown evidence, rather
    # than a compiler crash or an invented mode preference.
    ledger._path('artifact', manifest['groups']['$artifact']).unlink()
    state, rows = evidence(store, 104)
    assert not rows and state['excluded']['missing_context'] == 1
