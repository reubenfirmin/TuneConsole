"""Detached choice rules and narrow command ports; SQLite is forbidden here."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from threading import RLock
from unittest.mock import Mock, call

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.choice_context import ChoiceContexts
from yt_playlist.controllers.choice_evidence import ChoiceEvidence
from yt_playlist.controllers.choice_learning import ChoiceLearning
from yt_playlist.controllers.feedback_publication import FeedbackPublication
from yt_playlist.controllers.group_preparation import GroupPreparation
from yt_playlist.controllers.pending_roots import PendingRoots
from yt_playlist.controllers.choice_storage import ChoiceStorage
from yt_playlist.controllers.playback_choices import PlaybackChoices
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.rec import choice_context, choice_evidence, group_learning
from yt_playlist.rec.group_preparation import Budget

pytestmark = pytest.mark.usefixtures('no_database')
PARAMETERS = dict(choice=.25, open=.1, start=.2, completion=.3, promotion=.4,
                  early_exit=.1, window_d=30, match_threshold=.5)
HEAD = {'generation': 'current', 'groups': [dict(group_id='broad', version='v', parent_version=None,
    leaf=True, size=3, genres=[('House', 3)])]}


def offer(**values):
    return dict(id='offer', format='mixes', at=100, observed_at=100, context_at=100,
                options=[{'id': 'one', 'descriptor_id': 'missing'}], **values)


def visible(at=101):
    return dict(id='visible', opportunity_id='offer', kind='visible', at=at, observed_at=at,
                option_id=None, visible=['one'], details={})


def learner(expand):
    return ChoiceLearning(expand=expand, parameters=lambda: PARAMETERS, modes=lambda: [], axes=lambda _: {},
                          rating_advances=lambda _: {})


@pytest.mark.parametrize('condition', ['expired', 'unseen', 'future-context', 'future-observation'])
def test_inadmissible_offer_never_acquires_a_missing_descriptor(condition):
    row, responses, now = offer(), [visible()], 103
    if condition == 'expired':
        now = 31 * 86400
    elif condition == 'unseen':
        responses = []
    elif condition == 'future-context':
        row['context_at'] = 101
    else:
        responses[0]['observed_at'] = 104
    expand = Mock(side_effect=ValueError('Missing mix choice descriptor'))
    state = learner(expand).groups({'offer': row}, responses, now, HEAD)
    assert state['offered'] == 0 and state['picked'] == 0
    expand.assert_not_called()


def test_admitted_missing_descriptor_fails_before_inventing_learning():
    expand = Mock(side_effect=ValueError('Missing mix choice descriptor'))
    with pytest.raises(ValueError, match='Missing mix choice descriptor'):
        learner(expand).groups({'offer': offer()}, [visible()], 103, HEAD)
    expand.assert_called_once()


def test_held_menu_acquires_descriptor_once_and_daily_mass_remains_bounded():
    options = [dict(id='one', genres=['House'], genres_complete=True)]
    expand = Mock(return_value=options)
    choice = dict(id='first', opportunity_id='offer', kind='choice', at=102, observed_at=102,
                  option_id='one', details={})
    inputs = {'offer': offer()}, [visible(), choice, {**choice, 'id': 'again', 'at': 103, 'observed_at': 103}]
    original = deepcopy(inputs)
    result = learner(expand).groups(*inputs, 104, HEAD)
    assert result['rows'][0]['positive'] == .25
    assert result['rows'][0]['days'] == 1
    assert inputs == original
    expand.assert_called_once()


def test_context_acquires_unique_components_before_pure_restoration_and_keeps_nested_payload_opaque():
    manifest = {'a': {'$artifact': 'part'}, 'b': {'$artifact': 'part'}}
    component = {'untouched': {'$artifact': 'not-a-manifest-reference'}}
    ledger = Mock(spec_set=['get', 'artifact'])
    ledger.get.side_effect = lambda kind, identity: {'root': manifest, 'part': component}[identity]
    result = ChoiceContexts(ledger).resolve({'artifact': 'root'})
    assert result == {'a': component, 'b': component}
    assert ledger.get.call_args_list == [call('artifact', 'root'), call('artifact', 'part')]
    assert choice_context.resolve_context(manifest, {'part': component}) == result
    with pytest.raises(ValueError, match='no longer retained'):
        choice_context.resolve_context(manifest, {})


def test_mix_context_projects_large_unused_fields_before_publishing_any_artifact():
    ledger = Mock(spec_set=['get', 'artifact'])
    captured = {'at': 100, 'musical_groups': ['large group definition' * 4000]}
    ledger.get.return_value = captured
    contexts = ChoiceContexts(ledger)
    inline = contexts.mix(captured)
    assert inline == {'schema': 2, 'precision': 'theme-raw-genres-v1', 'at': 100}
    ledger.get.assert_not_called()
    assert contexts.mix({'artifact': 'legacy', 'at': 101}) == {**inline, 'at': 101}
    ledger.get.assert_called_once_with('artifact', 'legacy')
    ledger.artifact.assert_not_called()


def test_response_retry_keeps_original_clock_without_republishing_or_retaining_again():
    ledger = Mock(spec_set=['get', 'put'])
    ledger.get.side_effect = [dict(id='offer', format='mixes', at=100, context='original',
        options=[{'id': 'one'}]), dict(opportunity_id='offer', kind='choice', option_id='one', at=101)]
    retain = Mock()
    command = ChoiceEvidence(ledger, publish_mix=Mock(), retain=retain)
    assert command.respond('offer', 'choice', 110, option_id='one', action_id='action', visible=(), details={}) is False
    ledger.put.assert_not_called()
    retain.assert_not_called()


@pytest.mark.parametrize('stored', [None, 'different', 'matching'])
def test_feedback_acknowledges_only_confirmed_matching_publication(stored):
    row = dict(id=7, opportunity_id='offer', kind='feedback', observed_at=100,
        option_id='original-release', action_id='action', feedback_kind='more')
    receipt = dict(opportunity_id='offer', kind='feedback', option_id='original-release',
                   details={'kind': 'more'}, at=101 if stored == 'different' else 100)
    repository = Mock(spec_set=['pending_responses', 'acknowledge_response'])
    repository.pending_responses.return_value = [row]
    advance = Mock()
    command = FeedbackPublication(repository, respond=Mock(return_value=False),
        evidence=Mock(return_value=None if stored is None else receipt), paused=lambda: False,
        cursor=lambda: 3, advance=advance)
    if stored == 'different':
        with pytest.raises(ValueError, match='differs from its durable intent'):
            command.drain(limit=4)
    else:
        assert command.drain(limit=4) == int(stored == 'matching')
    assert repository.acknowledge_response.call_count == int(stored == 'matching')
    advance.assert_called_once_with(7)


def test_pending_root_failed_release_leaves_reapable_marker_and_close_is_idempotent():
    marker = Mock(spec_set=['close'])
    files = Mock(spec_set=['reserve', 'claim'])
    files.reserve.return_value = marker
    callbacks = []
    command = PendingRoots(files, admission=lambda **kw: nullcontext(), writer=nullcontext,
        retain=Mock(return_value=True), release=Mock(side_effect=WorkBudgetExceeded('busy')), before_close=callbacks)
    captured, close = command.capture(lambda: {'original': True}, 100)
    assert captured == {'original': True} and callbacks == [close]
    close()
    close()
    assert callbacks == []
    marker.close.assert_called_once_with()
    command.release.assert_called_once()


@pytest.mark.parametrize('accepted', [False, True])
def test_group_generation_guard_controls_final_publication(accepted):
    groups = Mock()
    groups.staged_tables = frozenset({'staging'})
    groups.head_generation.return_value = 'previous'
    groups.stage.return_value = 'owner'
    snapshot = SimpleNamespace(publish_ordered=Mock(side_effect=lambda target, **kw: (target() or True) if accepted else False))
    command = GroupPreparation(groups, Mock(), Mock(), Mock(), capture=Mock(), report=Mock(),
                               policy=Mock(), budget=Budget())
    command._pending = snapshot, dict(generation='captured', serial=4)
    assert command._publish_pending(100) is accepted
    assert command._pending is None
    assert groups.finish_preparation.call_count == int(accepted)
    assert groups.abandon_staged.call_count == int(not accepted)
    if accepted:
        groups.finish_preparation.assert_called_once_with('captured', 'owner', 4, 100)


def test_uncertain_head_publication_finishes_checkpoint_without_restaging():
    groups = Mock()
    groups.head_generation.return_value = 'captured'
    command = GroupPreparation(groups, Mock(), Mock(), Mock(), capture=Mock(), report=Mock(),
                               policy=Mock(), budget=Budget())
    command._pending = Mock(), dict(generation='captured', serial=4)
    assert command._publish_pending(100)
    groups.stage.assert_not_called()
    groups.checkpoint_preparation.assert_called_once_with('captured', 4, 100)


@pytest.mark.parametrize('deferred', [False, True])
def test_storage_intent_uses_evidence_owned_writes_without_an_outer_transaction(monkeypatch, deferred):
    evidence_files = Mock(spec_set=['update_maintenance'])
    store = SimpleNamespace(_evidence_state_lock=RLock(), _evidence_maintenance=0, evidence_files=evidence_files)
    monkeypatch.setattr(choice_evidence.group_roots, 'writer',
                        Mock(side_effect=AssertionError('Storage intent commits in its repository')))
    monkeypatch.setattr(choice_evidence.group_roots, 'admission', lambda _: nullcontext())
    monkeypatch.setattr(choice_evidence, '_maintain_storage', lambda *args, **kwargs: {'deferred': deferred})
    assert choice_evidence.maintain_storage(store, 100) == {'deferred': deferred}
    expected = [call(pending=True)]
    if not deferred:
        expected.append(call(pending=False, phase='inventory'))
    assert evidence_files.update_maintenance.call_args_list == expected
    assert store._evidence_recovery_pending is deferred
    assert store._evidence_maintenance == 0


def test_detached_group_state_rejects_future_and_expired_reports():
    state = dict(policy=group_learning.POLICY, generation='g', parameters=PARAMETERS, at=100, expires_at=200)
    assert group_learning.verified_state(state, 'g', 101, PARAMETERS) is state
    assert group_learning.verified_state(state, 'g', 99, PARAMETERS) == {}
    assert group_learning.verified_state(state, 'g', 200, PARAMETERS) == {}


def test_storage_resumes_checkpoint_and_releases_protection_before_collection():
    from contextlib import contextmanager

    operations = Mock()
    evidence_files = Mock(maintenance_state=Mock(return_value={'phase': 'checkpoint'}))
    protected_reader = False

    @contextmanager
    def capture():
        nonlocal protected_reader
        protected_reader = True
        yield SimpleNamespace(capture=lambda: ({'component'}, {'offer'}))
        protected_reader = False

    def collect(*args, **kw):
        assert not protected_reader
        assert kw['protected_artifacts'] == {'pending', 'explicit', 'component'}
        assert kw['protected_opportunities'] == {'queued', 'offer'}
        return {'collected': 1}

    ledger = SimpleNamespace(byte_budget=1000, maintain=Mock(side_effect=collect))
    command = ChoiceStorage(ledger, evidence_files, inventory=operations.inventory,
        acknowledge=operations.acknowledge, more=lambda: False, capture=capture,
        pending=lambda: ({'pending'}, {'queued'}), protected=lambda: False,
        pending_protection=nullcontext, retired=operations.retired, phase=operations.phase,
        progress=operations.progress, retention_days=lambda: 90, diagnostic_days=1, diagnostic_fraction=.1)
    assert command.run(100, protected_artifacts={'explicit'}) == {'collected': 1}
    operations.inventory.assert_not_called()
    operations.acknowledge.assert_called_once_with(100)
    evidence_files.update_maintenance.assert_called_once_with(phase='retention')


def test_playback_requires_a_verified_option_and_uses_once_only_player_identity():
    ledger = Mock(spec_set=['choice_playback', 'bind_choice_playback'])
    response = Mock(return_value=True)
    command = PlaybackChoices(ledger, paused=lambda: False,
        opportunity=lambda identity: {'options': [{'id': 'offered'}]}, respond=response, horizon=lambda: 3600)
    command.bind(1, 'playlist', 'video', 'offer', 'absent', 100)
    ledger.bind_choice_playback.assert_not_called()
    command.bind(1, 'playlist', 'video', 'offer', 'offered', 100)
    ledger.bind_choice_playback.assert_called_once_with(1, 'playlist', 'video', 'offer', 'offered', 100, 3600)
    ledger.choice_playback.return_value = dict(opportunity_id='offer', option_id='offered', at=100)
    assert command.observe(1, 'playlist', 'video', 'ended', 200, 200, 'event', 300)
    assert response.call_args.kwargs['action_id'] == 'player:event'
    assert response.call_args.kwargs['details']['start_at'] == 100


def test_album_capture_defers_response_acquisition_until_a_response_is_needed(monkeypatch):
    from yt_playlist.library import choice_composition
    from yt_playlist.rec import rec_params

    pending = dict(at=100, account_id=1, playlist='queue', browse='album', videos=['video'])
    reader = SimpleNamespace(rec_ledger=Mock(spec_set=['album_playback_context']))
    reader.rec_ledger.album_playback_context.return_value = pending
    ledger = Mock(spec_set=['bind_choice_playback', 'choice_playback'])
    ledger.choice_playback.return_value = dict(opportunity_id='offer', option_id='one', at=100)
    store = SimpleNamespace(rec_ledger=ledger, read_view=Mock(return_value=nullcontext(reader)),
        collection=Mock(spec_set=['playback_source']), playlist_recipes=Mock(spec_set=['get_recipe']),
        history=Mock(spec_set=['browse_playback_onset']))
    observations = Mock(spec_set=['opportunity', 'safe_respond'])
    observations.opportunity.return_value = {'options': [{'id': 'one'}]}
    observations.safe_respond.return_value = True
    acquire = Mock(return_value=observations)
    monkeypatch.setattr(choice_composition, 'responses', acquire)
    parameter = Mock(return_value=1)
    monkeypatch.setattr(rec_params, 'get_param', parameter)

    command = choice_composition.playback(store)
    assert command.album_context(dict(playlist='queue', videoId='video'), 110, account_id=1) is pending
    reader.rec_ledger.album_playback_context.assert_called_once_with()
    parameter.assert_called_once_with(reader, 'choice_attribution_h')
    acquire.assert_not_called()

    command.bind(1, 'queue', 'video', 'offer', 'one', 110)
    assert command.observe(1, 'queue', 'video', 'ended', 200, 200, 'event', 120)
    acquire.assert_called_once_with(store)
    ledger.bind_choice_playback.assert_called_once_with(1, 'queue', 'video', 'offer', 'one', 110, 3600)
    observations.safe_respond.assert_called_once()


def test_raw_sql_snapshot_constructs_no_repositories_and_closes_after_failure():
    import threading
    from yt_playlist.core.db_reads import read_sql_snapshot

    connection = Mock()
    source = SimpleNamespace(_db_path='private.db', _retained_readers=nullcontext,
        _make_reader=Mock(return_value=SimpleNamespace(conn=connection)), _lock=threading.RLock(),
        conn=SimpleNamespace(in_transaction=False), priority=SimpleNamespace(is_background=False),
        _analytics_readers=threading.BoundedSemaphore(1), _reader_diagnostics=SimpleNamespace(
            owner=lambda background: nullcontext()))
    with pytest.raises(ValueError, match='capture failed'):
        with read_sql_snapshot(source) as reader:
            assert reader is connection
            raise ValueError('capture failed')
    source._make_reader.assert_called_once_with(bind_repositories=False)
    assert connection.execute.call_args_list == [call('BEGIN'), call('SELECT 1 FROM settings LIMIT 1')]
    connection.close.assert_called_once()


def test_replay_rejects_future_context_before_loading_options_and_preserves_observation_order():
    from yt_playlist.controllers.choice_replay import ChoiceReplay
    from yt_playlist.core import mix_choice_policy as mix_choices

    offer = dict(id='offer', format='mixes', observed_at=10, at=10, context='context',
                 options=[{'id': 'chosen'}], prediction=['chosen'], episode_id='episode')
    visible = dict(id='visible', observed_at=11, at=11, kind='visible', opportunity_id='offer',
                   visible=['chosen'], details={})
    chosen = dict(id='choice', observed_at=12, at=12, kind='choice', opportunity_id='offer',
                  option_id='chosen', details={})
    ledger = Mock()
    ledger.records.side_effect = lambda kind: [offer] if kind == 'opportunity' else [visible, chosen]
    calls = []
    predictor = SimpleNamespace(mix_precision=mix_choices.PRECISION,
        predict=lambda context, options: calls.append('predict') or ['chosen'],
        observe=lambda event: calls.append(event['id']))
    expand = Mock(return_value=offer['options'])
    resolve = Mock(return_value={'at': 20})
    command = ChoiceReplay(ledger, resolve=resolve, learning=Mock(), checkpoint=Mock(), expand=expand)
    result = command.run(30, predictor)
    assert result['formats']['mixes']['future_context'] == 1
    expand.assert_not_called()
    assert calls == ['visible', 'choice']
    calls.clear()
    resolve.return_value = {'at': 10}
    result = command.run(30, predictor)
    assert result['formats']['mixes']['hits'] == 1
    assert calls == ['predict', 'visible', 'choice']


@pytest.mark.parametrize('change', [{'at': 200}, {'account_id': 2}, {'videos': ['other']}])
def test_album_attribution_rejects_future_account_and_video_mismatches(change):
    from yt_playlist.rec.choice_outcomes import album_context
    pending = dict(at=100, account_id=1, playlist='queue', browse='album', videos=['video'])
    message = dict(playlist='queue', videoId='video')
    assert album_context({**pending, **change}, message, 110, account_id=1, horizon=3600) is None
    assert album_context(pending, message, 110, account_id=1, horizon=3600) is pending
