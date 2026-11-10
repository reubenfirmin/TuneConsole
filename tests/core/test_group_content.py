"""Distinct group content can survive new publications without losing its inputs."""
import copy
import json

import pytest

from yt_playlist.core import group_content as content


def prepared(generation, at):
    parent, leaf = generation + '-parent', generation + '-leaf'
    return {'generation': generation, 'at': at, 'serial': int(at),
            'fingerprint': 'publication-fingerprint-' + generation,
            'policy': '{"algorithm":"grouping-1","threshold":0.8}',
            'payload': json.dumps({'recording_inputs': {'one': '1' * 64, 'two': '2' * 64},
                                   'last_structure_at': at, 'diagnostics': {'elapsed': at}}),
            'groups': [
                {'group_id': 'stable-parent', 'version': parent, 'parent_version': None,
                 'payload': '{"name":"Parent","leaf":false,"membership_known":true}',
                 'members': [('one', .9, .8), ('two', .7, .6)]},
                {'group_id': 'stable-leaf', 'version': leaf, 'parent_version': parent,
                 'payload': '{"name":"Leaf","leaf":true,"membership_known":true}',
                 'members': [('one', .9, .8)]}]}


def rows(value, index=1, resolve=None):
    return content.prepared_rows(value, value['groups'][index],
                                 resolve or {'one': 1, 'two': 2}.get)


def test_new_generation_and_parent_version_can_reuse_identical_group_content():
    old, new = prepared('old', 1), prepared('new', 20)
    assert content.digest(rows(old)) == content.digest(rows(new))
    assert content.equal(rows(old), rows(new))
    assert old['groups'][1]['version'] != new['groups'][1]['version']
    assert old['groups'][1]['parent_version'] != new['groups'][1]['parent_version']


@pytest.mark.parametrize('change', ['score', 'support', 'member', 'input', 'policy', 'name', 'parent', 'identity'])
def test_every_semantic_change_prevents_reusing_a_group_version(change):
    old = prepared('old', 1)
    new = copy.deepcopy(old)
    group = new['groups'][1]
    if change == 'score':
        group['members'][0] = ('one', .8, .8)
    elif change == 'support':
        group['members'][0] = ('one', .9, .7)
    elif change == 'member':
        group['members'].append(('two', .9, .8))
    elif change == 'input':
        value = json.loads(new['payload'])
        value['recording_inputs']['one'] = '3' * 64
        new['payload'] = json.dumps(value)
    elif change == 'policy':
        new['policy'] = '{"algorithm":"grouping-2","threshold":0.8}'
    elif change == 'name':
        group['payload'] = '{"name":"Renamed","leaf":true,"membership_known":true}'
    elif change == 'parent':
        new['groups'][0]['group_id'] = 'another-parent'
    else:
        group['group_id'] = 'another-leaf'
    assert content.digest(rows(old)) != content.digest(rows(new))
    assert not content.equal(rows(old), rows(new))


def test_nonmember_input_changes_do_not_duplicate_unchanged_leaf_content():
    old, new = prepared('old', 1), prepared('new', 20)
    payload = json.loads(new['payload'])
    payload['recording_inputs']['two'] = '3' * 64
    new['payload'] = json.dumps(payload)
    assert content.equal(rows(old), rows(new))
    assert not content.equal(rows(old, 0), rows(new, 0))


def test_unknown_member_input_is_distinct_from_known_evidence():
    old = prepared('old', 1)
    new = copy.deepcopy(old)
    payload = json.loads(new['payload'])
    del payload['recording_inputs']['one']
    new['payload'] = json.dumps(payload)
    assert not content.equal(rows(old), rows(new))


def test_missing_recording_cannot_be_deduplicated_as_a_known_owner():
    value = prepared('old', 1)
    assert not content.equal(rows(value), rows(value, resolve=lambda _key: None))
    assert not content.equal(rows(value), rows(value, resolve=lambda _key: 2))


def test_equality_verification_detects_changed_payload_and_truncated_members():
    left = list(rows(prepared('old', 1), 0))
    assert content.equal(iter(left), iter(left))
    assert not content.equal(iter(left), iter(left[:-1]))
    assert not content.equal(iter(left), iter([left[0], left[-1]]))


@pytest.mark.parametrize('value', [True, float('nan'), float('inf'), -0.01, 1.01])
def test_invalid_membership_scores_are_refused(value):
    prepared_value = prepared('old', 1)
    prepared_value['groups'][1]['members'] = [('one', value, .8)]
    with pytest.raises(ValueError):
        content.digest(rows(prepared_value))


@pytest.mark.parametrize('change', ['duplicate', 'missing_parent', 'invalid_input', 'invalid_owner'])
def test_invalid_content_cannot_produce_a_reusable_digest(change):
    value = prepared('old', 1)
    resolve = None
    if change == 'duplicate':
        value['groups'][1]['members'] *= 2
    elif change == 'missing_parent':
        value['groups'][1]['parent_version'] = 'absent'
    elif change == 'invalid_input':
        value['payload'] = '{"recording_inputs":{"one":"unknown"}}'
    else:
        resolve = lambda _key: True
    with pytest.raises(ValueError):
        content.digest(rows(value, resolve=resolve))
