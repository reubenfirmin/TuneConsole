"""Release feedback follows verified aliases, while original observations stay intact."""
from copy import deepcopy
import json

import numpy as np
import pytest

from tests.repos.test_album_aliases import OLD, ALIAS, release
from yt_playlist.rec import album_choices, choice_evidence, scoring


@pytest.mark.parametrize('kind', ['reject', 'known', 'more'])
@pytest.mark.parametrize('before', [True, False])
@pytest.mark.parametrize('source,other', [(OLD, ALIAS), (ALIAS, OLD)])
def test_feedback_follows_verified_release_and_clears_through_either_id(store, kind, before, source, other):
    if before:
        album_choices.set_feedback(store, source, kind, 100)
    for browse in (OLD, ALIAS):
        store.collection.observe_album_representation(browse, release(), now=101)
    if not before:
        album_choices.set_feedback(store, source, kind, 102)
    original = store.settings.get_setting('album_feedback')
    states = album_choices.feedback_state(store, [OLD, ALIAS])
    assert states[OLD] == states[ALIAS] == json.loads(original)[source]
    assert list(album_choices.feedback_state(store)) == [OLD]
    assert store.settings.get_setting('album_feedback') == original
    prepared = [{'browse_id': browse, 'artist': 'All Them Witches', 'title': release()['title'],
                 'classification': 'discovery'} for browse in (OLD, ALIAS)]
    eligible = album_choices.eligible(store, prepared, 103)
    assert [item['classification'] for item in eligible] == (
        [] if kind == 'reject' else ['known', 'known'] if kind == 'known' else ['discovery', 'discovery'])
    album_choices.set_feedback(store, other, 'clear', 104)
    assert album_choices.feedback_state(store, [OLD, ALIAS]) == {}
    assert [item['classification'] for item in album_choices.eligible(store, prepared, 105)] == [
        'discovery', 'discovery']


@pytest.mark.parametrize('same_time', [False, True])
@pytest.mark.parametrize('reverse_order', [False, True])
def test_conflicts_resolve_by_time_then_source_and_replacement_withdraws_all_original_actions(
        store, same_time, reverse_order):
    actions = [(OLD, 'known', 100), (ALIAS, 'reject', 100 if same_time else 101)]
    if reverse_order:
        actions.reverse()
    for browse, kind, at in actions:
        oid = choice_evidence.issue(store, 'albums', [{'id': browse}], 90,
                                     context={}, opportunity_id=browse)
        album_choices.set_feedback(store, browse, kind, at, oid)
    original = json.loads(store.settings.get_setting('album_feedback'))
    responses = deepcopy(store.rec_ledger.evidence().records('response'))
    opportunities = deepcopy(store.rec_ledger.evidence().records('opportunity'))
    for browse in (OLD, ALIAS):
        store.collection.observe_album_representation(browse, release(), now=102)
    expected = max(original, key=lambda browse: (original[browse]['at'], browse))
    assert album_choices.feedback_state(store) == {OLD: original[expected]}
    assert json.loads(store.settings.get_setting('album_feedback')) == original
    # Replacing a judgment must not leave the losing pre-merge action able to reappear.
    album_choices.set_feedback(store, ALIAS, 'more', 103, ALIAS)
    assert set(json.loads(store.settings.get_setting('album_feedback'))) == {ALIAS}
    ledger = store.rec_ledger.evidence()
    assert ledger.records('opportunity') == opportunities
    assert all(ledger.get('response', row['id']) == row for row in responses)
    withdrawals = [row for row in ledger.records('response') if row['kind'] == 'withdraw']
    assert {row['details']['action_id'] for row in withdrawals} == {row['id'] for row in responses}
    latest = [row for row in ledger.records('response') if row['kind'] == 'feedback' and row['at'] == 103]
    assert len(latest) == 1 and latest[0]['option_id'] == ALIAS
    album_choices.set_feedback(store, OLD, 'clear', 104)
    assert album_choices.feedback_state(store) == {}
    assert {row['details']['action_id'] for row in ledger.records('response') if row['kind'] == 'withdraw'} == {
        row['id'] for row in responses + latest}


def test_same_title_distinct_editions_and_unknown_ids_keep_independent_feedback(store):
    changed = release()
    changed['tracks'][0]['videoId'] = 'different-edition'
    store.collection.observe_album_representation(OLD, release())
    store.collection.observe_album_representation(ALIAS, changed)
    album_choices.set_feedback(store, OLD, 'reject', 100)
    album_choices.set_feedback(store, 'unresolved', 'known', 101)
    assert album_choices.feedback_state(store, [OLD, ALIAS, 'unresolved', 'missing']) == {
        OLD: album_choices.feedback_state(store)[OLD],
        'unresolved': album_choices.feedback_state(store)['unresolved']}
    album_choices.set_feedback(store, ALIAS, 'clear', 102)
    assert set(album_choices.feedback_state(store)) == {OLD, 'unresolved'}


def test_taste_and_preparation_use_alias_feedback_once_and_restore_after_clear(store):
    from tests.rec.test_album_interests import album
    from yt_playlist.library.album_catalog import remember_album
    for browse in (OLD, ALIAS):
        remember_album(store, browse, release())
    store.collection.add_saved_album({'browse': OLD, 'title': release()['title'], 'artist': 'All Them Witches'})
    other_keys = album(store, 'another', 'Other artist', 'Folk', 2)
    keys = [f'track {i}|all them witches' for i in range(9)]
    store.rec_model.replace_rec_vectors([
        (key, np.array([1., 0.], dtype=np.float32).tobytes()) for key in keys] + [
        (key, np.array([0., 1.], dtype=np.float32).tobytes()) for key in other_keys])
    before = scoring.playlist_taste(store)
    assert len(before.titles) == 2
    album_choices.set_feedback(store, ALIAS, 'more', 100)
    weighted = scoring.playlist_taste(store)
    assert len(weighted.titles) == 2
    target = next(i for i, row in enumerate(weighted.sources) if row.get('browse') == OLD)
    assert weighted.sources[target]['feedback'] == 'more'
    assert weighted.weights[target] > before.weights[target]
    album_choices.set_feedback(store, ALIAS, 'reject', 101)
    assert scoring.playlist_taste(store).titles == ['Other artist · Other artist']
    assert [item['browse_id'] for item in album_choices.prepare(store, 102, [])] == ['another']
    album_choices.set_feedback(store, OLD, 'clear', 103)
    assert scoring.playlist_taste(store).weights == pytest.approx(before.weights)


def test_choice_taste_checks_alias_feedback_without_rewriting_option_ids(store, monkeypatch):
    from yt_playlist.rec import choice_learning
    from yt_playlist.rec.taste_context import PlaylistTaste
    for browse in (OLD, ALIAS):
        store.collection.observe_album_representation(browse, release())
    interest = {'format': 'albums', 'option_id': ALIAS, 'keys': ['track'], 'weight': 1,
                'days': 1, 'episodes': 1, 'layer_weights': {}, 'at': 100}
    monkeypatch.setattr(choice_learning, 'contexts', lambda *args: [interest])
    empty = PlaylistTaste([], np.zeros((0, 2)), np.zeros(0))
    album_choices.set_feedback(store, OLD, 'reject', 101)
    assert not scoring._with_choices(store, empty, np.array([[1., 0.]]), {'track': 0})
    album_choices.set_feedback(store, ALIAS, 'clear', 102)
    accepted = scoring._with_choices(store, empty, np.array([[1., 0.]]), {'track': 0})
    assert accepted.sources[0]['id'] == ALIAS
    saved = PlaylistTaste(['Saved'], np.array([[1., 0.]]), np.ones(1), [None])
    saved.sources = [{'kind': 'album', 'browse': OLD}]
    assert scoring._with_choices(store, saved, np.array([[1., 0.]]), {'track': 0}).titles == ['Saved']
