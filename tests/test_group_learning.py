from copy import deepcopy
from dataclasses import replace
import os

import pytest

from tests.group_support import publish, recordings
from yt_playlist.rec import choice_evidence as choices, group_learning


def menu(store, head, ids, *, at=1100, oid='groups'):
    options = [{'id': group['group_id'], 'group_version': group['version'], 'position': index + 1}
               for index, group in enumerate(head['groups']) if group['group_id'] in ids]
    context = {'schema': 1, 'at': at, 'group_generation': head['generation'],
               'musical_groups': [{key: group.get(key) for key in ('group_id', 'version', 'parent_version', 'name')}
                                  for group in head['groups'] if not group.get('legacy')]}
    return choices.issue(store, 'mixes', options, at, context=context, opportunity_id=oid)


def compiled(store, head, now):
    ledger = store.rec_ledger.evidence()
    offers = {row['id']: row for row in ledger.records('opportunity')}
    responses = sorted(ledger.records('response'), key=lambda row: (row['observed_at'], row['id']))
    result = group_learning.compile(store, offers, responses, now, head)
    return result, {row['group_id']: row for row in result['rows']}


def test_parent_feedback_is_a_bounded_prior_and_is_not_copied_into_children(store):
    tracks = recordings([('House', 35, .1), ('House', 35, .95)])
    head = publish(store, tracks)
    root = head['groups'][0]
    leaves = [group for group in head['groups'] if group['leaf']]
    oid = menu(store, head, [root['group_id']])
    choices.respond(store, oid, 'visible', 1101, visible=[root['group_id']])
    choices.respond(store, oid, 'choice', 1102, option_id=root['group_id'])
    _state, rows = compiled(store, head, 1103)
    assert rows[root['group_id']]['positive'] == .25
    for child in leaves:
        row = rows[child['group_id']]
        assert row['positive'] == row['negative'] == 0
        assert 0 < row['shared_strength'] <= 1
        assert row['estimate'] > .5


def test_siblings_share_one_parent_opportunity_without_self_reinforcement(store):
    head = publish(store, recordings([('House', 35, .1), ('House', 35, .95)]))
    leaves = [group for group in head['groups'] if group['leaf']]
    ids = [group['group_id'] for group in leaves]
    oid = menu(store, head, ids)
    choices.respond(store, oid, 'visible', 1101, visible=ids)
    choices.respond(store, oid, 'choice', 1102, option_id=ids[0])
    _state, rows = compiled(store, head, 1103)
    assert rows[ids[0]]['positive'] == .25
    assert rows[ids[1]]['negative'] == .25
    parent = rows[head['groups'][0]['group_id']]
    assert parent['positive'] == .25 and parent['negative'] == 0
    assert all(rows[gid]['shared_strength'] == 0 for gid in ids)


def test_missing_visibility_and_outcomes_are_not_rejections(store):
    head = publish(store, recordings([('House', 8, .3)]))
    gid = head['groups'][0]['group_id']
    oid = menu(store, head, [gid])
    _state, rows = compiled(store, head, 1101)
    assert rows[gid]['positive'] == rows[gid]['negative'] == 0
    choices.respond(store, oid, 'visible', 1102, visible=[gid])
    _state, rows = compiled(store, head, 1103)
    assert rows[gid]['positive'] == rows[gid]['negative'] == 0


def test_regrouped_parent_keeps_history_without_inventing_child_history(store):
    from yt_playlist.rec.group_discovery import Policy
    tracks = recordings([('House', 35, .1), ('House', 35, .95)])
    first = publish(store, tracks, policy=Policy(max_depth=0))
    gid = first['groups'][0]['group_id']
    oid = menu(store, first, [gid])
    choices.respond(store, oid, 'visible', 1101, visible=[gid])
    choices.respond(store, oid, 'choice', 1102, option_id=gid)
    second = publish(store, tracks, 1200, first)
    _state, rows = compiled(store, second, 1201)
    assert rows[gid]['positive'] == .25
    children = [row for key, row in rows.items() if key != gid]
    assert children and all(row['positive'] == 0 and row['shared_strength'] > 0 for row in children)


def test_extra_siblings_do_not_buy_extra_broad_area_allocation():
    root = {'group_id': 'root', 'version': 'root-v', 'parent_version': None, 'size': 100}
    a = {'group_id': 'a', 'version': 'a-v', 'parent_version': 'root-v', 'size': 50}
    b = {'group_id': 'b', 'version': 'b-v', 'parent_version': 'root-v', 'size': 50}
    first = group_learning.allocation({'groups': [root, a, b]})
    children = [{'group_id': f'a{i}', 'version': f'a{i}-v', 'parent_version': 'a-v', 'size': 10} for i in range(5)]
    second = group_learning.allocation({'groups': [root, a, b, *children]})
    assert first['a'] == sum(second[child['group_id']] for child in children)
    assert first['b'] == second['b'] == .5


def test_repeated_menus_on_one_day_cannot_multiply_group_feedback(store):
    head = publish(store, recordings([('House', 8, .3)]))
    gid = head['groups'][0]['group_id']
    for i in range(3):
        oid = menu(store, head, [gid], at=1100 + i * 10, oid=f'menu-{i}')
        choices.respond(store, oid, 'visible', 1101 + i * 10, visible=[gid])
        choices.respond(store, oid, 'choice', 1102 + i * 10, option_id=gid)
    _state, rows = compiled(store, head, 1200)
    assert rows[gid]['positive'] == .25 and rows[gid]['days'] == 1


def test_a_definition_created_after_the_served_context_cannot_receive_its_response(store):
    head = publish(store, recordings([('House', 8, .3)]), now=1000)
    gid = head['groups'][0]['group_id']
    oid = menu(store, head, [gid], at=900)
    choices.respond(store, oid, 'visible', 901, visible=[gid])
    choices.respond(store, oid, 'choice', 902, option_id=gid)
    state, rows = compiled(store, head, 1100)
    assert rows[gid]['positive'] == rows[gid]['negative'] == 0
    assert state['excluded']['unmapped_definition'] > 0


def test_parent_continuation_wins_over_overlap_with_a_large_new_child(store):
    from yt_playlist.rec.group_discovery import Policy
    tracks = recordings([('House', 80, .1), ('House', 20, .95)])
    first = publish(store, tracks, policy=Policy(max_depth=0))
    gid = first['groups'][0]['group_id']
    oid = menu(store, first, [gid])
    choices.respond(store, oid, 'visible', 1101, visible=[gid])
    choices.respond(store, oid, 'choice', 1102, option_id=gid)
    second = publish(store, tracks, 1200, first)
    _state, rows = compiled(store, second, 1201)
    assert rows[gid]['positive'] == .25
    assert all(row['positive'] == 0 for key, row in rows.items() if key != gid)


@pytest.mark.parametrize('transition', ['continuation', 'rejoin'])
@pytest.mark.parametrize('source', ['parent', 'child'])
@pytest.mark.parametrize('storage', ['raw', 'acknowledged', 'legacy-checkpoint',
                                     'acknowledged-released', 'legacy-released'])
def test_frozen_broad_support_survives_child_rejoin(store, monkeypatch, transition, source, storage):
    from tests.test_group_preparation import seed
    from yt_playlist.rec import choice_bandit, choice_context, choice_retention, group_preparation

    tracks = recordings([('House', 3, .1)])
    seed(store, tracks)
    worker = group_preparation.Preparation(store)
    assert worker.step(1000)
    joined = store.taste_groups.head(members=True)
    broad_id = joined['groups'][0]['group_id']
    seed(store, [replace(tracks[-1], values=(.95,) * len(tracks[-1].values))])
    assert worker.step(1060)
    split = deepcopy(store.taste_groups.head(members=True))
    broad = next(g for g in split['groups'] if g['group_id'] == broad_id)
    children = [g for g in split['groups'] if g['leaf']]
    assert sorted(g['size'] for g in children) == [1, 2]
    selected = broad if source == 'parent' else next(g for g in children if g['size'] == 2)
    oid = menu(store, split, [selected['group_id']], at=1070)
    assert choices.respond(store, oid, 'visible', 1071, visible=[selected['group_id']])
    assert choices.respond(store, oid, 'choice', 1072, option_id=selected['group_id'])
    expected = choice_bandit.parameters(store)['choice']
    _state, before = compiled(store, split, 1073)
    assert before[broad_id]['positive'] == expected
    ledger = store.rec_ledger.evidence()
    original = {(kind, row['id']): ledger._path(kind, row['id']).read_bytes()
                for kind in ('opportunity', 'response') for row in ledger.records(kind)}
    definition = store.taste_groups.definition(selected['version'])
    checkpoint_bytes = None
    if storage != 'raw':
        correction = choice_retention.correction_context

        def legacy_context(context):
            result = correction(context)
            for field in ('musical_groups', 'musical_definitions'):
                for group in result.get(field, []):
                    group.pop('parent_version', None)
            return result

        with monkeypatch.context() as serializing:
            if storage.startswith('legacy'):
                serializing.setattr(choice_retention, 'correction_context', legacy_context)
            if storage.endswith('released'):
                serializing.setattr(choice_retention, 'DIAGNOSTIC_FRACTION', 0)
                for path in ledger.directory.glob('artifact/*/*.json'):
                    os.utime(path, (1, 1))
            choices.maintain_storage(store, 1074)
        identity = ledger.get('opportunity', oid)['context']
        saved = choice_context.checkpoint(ledger, identity)
        assert saved
        if storage.endswith('released'):
            assert ledger.get('artifact', identity) is None
        checkpoint_bytes = ledger._path('checkpoint', identity).read_bytes()
        frozen = {group['version']: group for group in
                  choice_context.learning_context(store, identity)['musical_definitions']}
        assert frozen[selected['version']]['parent_version'] == selected['parent_version']
        assert compiled(store, split, 1074)[1] == before

    if transition == 'rejoin':
        seed(store, [tracks[-1]])
    else:
        worker.request_rebuild()
    assert worker.step(1120)
    current = store.taste_groups.head(members=True)
    retained = next(g for g in current['groups'] if g['group_id'] == broad_id)
    assert set(retained['members']) == set(broad['members'])
    assert retained['leaf'] == (transition == 'rejoin')
    state, after = compiled(store, current, 1121)
    assert not any(state['excluded'].values())
    assert after[broad_id]['positive'] == expected
    assert after[broad_id]['negative'] == 0 and after[broad_id]['days'] == 1
    assert after[broad_id]['menus'] == [oid]
    assert after[broad_id]['offered'] == after[broad_id]['picked'] == (1 if source == 'parent' else 0)
    if transition == 'continuation':
        assert after[selected['group_id']]['positive'] == expected
    if source == 'parent':
        assert all(row['positive'] == row['negative'] == 0 for gid, row in after.items() if gid != broad_id)
    assert store.taste_groups.definitions(split['generation'], members=True) == split['groups']
    assert store.taste_groups.definition(selected['version']) == definition
    assert all(ledger._path(kind, identity).read_bytes() == raw for (kind, identity), raw in original.items())
    if checkpoint_bytes is not None:
        assert ledger._path('checkpoint', identity).read_bytes() == checkpoint_bytes
    # The retained contribution still expires through the normal evidence window.
    _expired, rows = compiled(store, current, state['expires_at'] + 1)
    assert rows[broad_id]['positive'] == rows[broad_id]['negative'] == rows[broad_id]['days'] == 0


def test_learning_policy_change_recompiles_unchanged_evidence(store, monkeypatch):
    publish(store, recordings([('House', 8, .3)]))
    choices.refresh_report(store, 1100)
    previous = store.rec_proposals.get_proposals('group_choice_bandit')
    assert previous['policy'] == group_learning.POLICY
    monkeypatch.setattr(group_learning, 'POLICY', 'changed-learning-policy')
    choices.refresh_report(store, 1101)
    current = store.rec_proposals.get_proposals('group_choice_bandit')
    assert current['at'] == 1101 and current['policy'] == group_learning.POLICY
    assert current['state_version'] != previous['state_version']
