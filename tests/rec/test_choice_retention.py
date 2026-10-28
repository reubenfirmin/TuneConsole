"""Correction and chronology guarantees after releasing heavy diagnostic inputs."""
import json
import os

import pytest

from tests.rec.test_choice_bandit import modes
from yt_playlist.core.store import Store
from yt_playlist.rec import choice_evidence as choices, choice_retention, comparison_storage
from yt_playlist.rec.choice_context import checkpoint, learning_context

DAY = 86400


def captured(store, oid='menu', at=100):
    groups = [{**mode, 'centroid': mode['centroid'].tolist()}
              for mode in store.modes.list_modes(active_only=True)]
    oid = choices.issue(store, 'mixes', [{'id': 'one', 'mode_id': 1, 'score': 1},
                                        {'id': 'two', 'mode_id': 2, 'score': 0}], at,
                        context={'at': at, 'groups': groups, 'layers': {'saved': oid * 40_000}},
                        opportunity_id=oid)
    choices.respond(store, oid, 'visible', at + 1, visible=['one', 'two'])
    choices.respond(store, oid, 'choice', at + 2, option_id='one', action_id=oid + ':pick')
    return store.rec_ledger.evidence().get('opportunity', oid)


def age_artifacts(store):
    for path in store.rec_ledger.evidence().directory.glob('artifact/*/*.json'):
        os.utime(path, (1, 1))


def test_compaction_preserves_learning_predictions_withdrawals_and_restart(tmp_path):
    path = tmp_path / 'library.db'
    store = Store(path)
    store.init_schema()
    modes(store)
    original = captured(store)
    before = choices.compile_report(store, 103)
    evaluated = choices.replay(store, 103)
    age_artifacts(store)
    result = choices.maintain_storage(store, 31 * DAY)
    ledger = store.rec_ledger.evidence()
    assert result['compacted_contexts'] == 1
    assert checkpoint(ledger, original['context'])['watermark'] == before['watermark']
    assert ledger.get('artifact', original['context']) is None
    assert ledger.get('opportunity', original['id']) == original
    assert learning_context(store, original['context'])['groups']
    assert choices.compile_report(store, 103)['bandit'] == before['bandit']
    assert choices.replay(store, 103) == evaluated
    store.conn.close()
    store = Store(path)
    store.init_schema()
    try:
        assert choices.compile_report(store, 103)['bandit'] == before['bandit']
        choices.respond(store, 'menu', 'withdraw', 32 * DAY, details={'action_id': 'menu:pick'})
        assert choices.compile_report(store, 32 * DAY)['formats']['mixes']['choices'] == 0
        assert choices.compile_report(store, 103)['formats']['mixes']['choices'] == 1
        assert not choices.respond(store, 'menu', 'choice', 33 * DAY, option_id='one', action_id='menu:pick')
        class Candidate:
            def predict(self, *args):
                pytest.fail('A released diagnostic context was passed to a custom predictor')
            def observe(self, event):
                pass
        replay = choices.replay(store, 103, Candidate())['formats']['mixes']
        assert replay['released_context'] == 1 and replay['scored'] == replay['future_context'] == 0
    finally:
        store.conn.close()


def test_failed_learning_publication_never_acknowledges_or_releases(store, monkeypatch):
    modes(store)
    offer = captured(store)
    age_artifacts(store)
    def fail(*args, **kwargs):
        raise OSError('publication failed')
    monkeypatch.setattr(store.rec_proposals, 'publish_choice_findings', fail)
    with pytest.raises(OSError, match='publication failed'):
        choices.maintain_storage(store, 31 * DAY)
    ledger = store.rec_ledger.evidence()
    assert checkpoint(ledger, offer['context']) is None
    assert ledger.get('artifact', offer['context'])


def test_optional_context_bytes_are_bounded_during_active_trial_and_held_context_survives(store, monkeypatch):
    modes(store)
    # Exercise the byte limit during a trial, well before the 30-day age limit.
    monkeypatch.setattr(choice_retention, 'DIAGNOSTIC_FRACTION', .003)
    ledger = store.rec_ledger.evidence()
    snapshots = []
    for index in range(15):
        offer = captured(store, oid=f'menu-{index}', at=(index + 1) * DAY)
        snapshots.append(offer['context'])
        age_artifacts(store)
        result = choices.maintain_storage(store, (index + 1) * DAY + 1000)
        assert result['optional_context_bytes'] <= ledger.byte_budget * .003
        assert len(ledger.records('checkpoint')) == index + 1
    assert len(ledger.records('opportunity')) == 15
    assert len(ledger.records('artifact')) < 6
    held = captured(store, oid='held', at=16 * DAY)
    store.rec_proposals.put_proposals('home_prepared', {'choice_context': {'artifact': held['context']}}, 16 * DAY)
    age_artifacts(store)
    choices.maintain_storage(store, 100 * DAY)
    assert ledger.get('artifact', held['context'])
    assert ledger.get('artifact', snapshots[0]) is None


def test_paired_storage_is_lossless_and_enforces_expanded_limit(monkeypatch):
    track = {'key': 'recording', 'score': .123, 'metadata': 'complete' * 1000, 'pool_policy': 'policy-id'}
    data = {'menus': [{'protos': [{'tracks': [track] * 12}]}] * 12,
            'context': {'artifact': 'frozen'}, 'tuning': 'current'}
    encoded = comparison_storage.encode(42, data)
    assert len(encoded) < len(json.dumps(data)) / 10
    payload = json.loads(encoded)
    assert payload['context'] == data['context']
    from yt_playlist.repos.policy_storage import references
    assert 'policy-id' in set(references(payload))
    assert comparison_storage.decode(payload) == data
    assert comparison_storage.decode({'run_id': 42, 'data': data}) == data
    with pytest.raises(ValueError):
        comparison_storage.decode({**payload, 'expanded_bytes': 10})
    monkeypatch.setattr(comparison_storage, 'MAX_EXPANDED_BYTES', 100)
    with pytest.raises(ValueError, match='expanded storage budget'):
        comparison_storage.encode(42, data)
