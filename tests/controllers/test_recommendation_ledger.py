from unittest.mock import create_autospec, Mock
from contextlib import nullcontext

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.recommendation_ledger import RecommendationLedger
from yt_playlist.controllers.policies import Policies
from yt_playlist.controllers.playback_contexts import PlaybackContexts
from yt_playlist.controllers.recommendation_impressions import RecommendationImpressions
from yt_playlist.repos.rec_ledger import RecLedgerRepo
from yt_playlist.repos.scorecard_attribution import ScorecardFacts

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.fixture
def ledger():
    repository = create_autospec(RecLedgerRepo, instance=True, spec_set=True)
    repository.scorecard_impressions.return_value = [{'id': 1, 'identity_key': 'song'}]
    facts = create_autospec(ScorecardFacts, instance=True, spec_set=True)
    facts.owners.return_value = []
    facts.outcomes.return_value = ([], [])
    facts.exits.return_value = []
    facts.video_owners.return_value = []
    facts.catalog_keys.return_value = []
    repository.scorecard_facts.return_value = facts
    return RecommendationLedger(repository, create_autospec(Policies, instance=True, spec_set=True), nullcontext,
                                ledger_factory=Mock(), playback=create_autospec(PlaybackContexts, instance=True),
                                impressions=create_autospec(RecommendationImpressions, instance=True))


def test_nearest_prior_impression_uses_last_id_at_equal_times_and_inclusive_horizon(ledger):
    facts = ledger.repository.scorecard_facts.return_value
    facts.owners.return_value = [
        {'id': 3, 'identity_key': 'song', 'served_at': 150},
        {'id': 1, 'identity_key': 'song', 'served_at': 100},
        {'id': 2, 'identity_key': 'song', 'served_at': 150}]
    _, attribution = ledger.scorecard_inputs(90, 200, outcome_window_h=1)
    assert attribution.nearest('song', 99) is None
    assert attribution.nearest('song', 149) == 1
    assert attribution.nearest('song', 150) == 3
    assert attribution.nearest('song', 3750) == 3
    assert attribution.nearest('song', 3751) is None
    assert attribution.nearest('unknown', 150) is None
    facts.owners.assert_called_once_with({'song'}, -3510., 3800., policy_kind='live')
    facts.outcomes.assert_called_once_with({'song'}, 90, 3800.)


def test_historical_video_owner_precedes_owned_then_discovery_fallback(ledger):
    facts = ledger.repository.scorecard_facts.return_value
    facts.exits.return_value = [
        {'video_id': video, 'at': 200} for video in ('historical', 'owned', 'discovered')]
    facts.video_owners.return_value = [
        {'id': 7, 'video_id': 'historical', 'identity_key': 'old-identity', 'served_at': 100}]
    facts.catalog_keys.side_effect = [
        [{'video_id': 'owned', 'identity_key': 'owned-identity'},
         {'video_id': 'discovered', 'identity_key': None}],
        [{'video_id': 'discovered', 'identity_key': 'discovery-identity'}]]
    _, attribution = ledger.scorecard_inputs(90, 110)
    assert attribution.exit_key('historical', 200) == 'old-identity'
    assert attribution.exit_key('owned', 200) == 'owned-identity'
    assert attribution.exit_key('discovered', 200) == 'discovery-identity'
    assert facts.catalog_keys.call_args_list[0].args == ({'owned', 'discovered'},)
    assert facts.catalog_keys.call_args_list[1].args == ({'discovered'},)
    assert facts.catalog_keys.call_args_list[0].kwargs == {'discovered': False}
    assert facts.catalog_keys.call_args_list[1].kwargs == {'discovered': True}


def test_empty_report_does_not_load_event_timelines(ledger):
    ledger.repository.scorecard_impressions.return_value = []
    impressions, attribution = ledger.scorecard_inputs(90, 110)
    assert impressions == attribution.plays == attribution.responses == attribution.exits == []
    ledger.repository.scorecard_facts.assert_not_called()


def test_failed_timeline_query_stops_loading_report_inputs(ledger):
    ledger.repository.scorecard_facts.return_value.owners.side_effect = RuntimeError('interrupted')
    with pytest.raises(RuntimeError, match='interrupted'):
        ledger.scorecard_inputs(90, 110)
    ledger.repository.scorecard_facts.return_value.outcomes.assert_not_called()


@pytest.mark.parametrize('current,cap,expected', [(0, 3, 4), (3, 3, 4), (4, 3, 7), (0, 0, 2)])
def test_refresh_advances_to_the_first_view_of_the_next_epoch(ledger, current, cap, expected):
    ledger.repository.card_views.return_value = current
    ledger.refresh_card('wheelhouse', cap, 200)
    ledger.repository.set_card_views.assert_called_once_with('wheelhouse', expected, 200)


def test_response_vocabulary_is_validated_before_persistence(ledger):
    with pytest.raises(ValueError, match='unsupported'):
        ledger.record_recommendation_outcomes('event', 'invented-response', ['song'], 200)
    ledger.repository.insert_recommendation_outcomes.assert_not_called()
    ledger.repository._prune_response_batch.assert_not_called()


def test_failed_event_write_does_not_start_retention(ledger):
    ledger.impressions.record.side_effect = RuntimeError('interrupted')
    with pytest.raises(RuntimeError, match='interrupted'):
        ledger.record_served_impressions('shown', 'home', [{'key': 'song', 'rank': 1}], 200, prune_before=100)
    ledger.repository._prune_impression_batch.assert_not_called()
    ledger.repository._prune_response_batch.assert_not_called()
    ledger.policies.prune.assert_not_called()


def test_policy_owners_commit_before_the_impression_write(ledger):
    events = []
    ledger.policies.publish.side_effect = lambda *args: events.append('policies')
    ledger.impressions.record.side_effect = lambda *args, **kwargs: events.append('impressions')
    ledger.record_served_impressions('shown', 'home', [{'key': 'song', 'rank': 1}], 200,
                                     policies=[('version', '{}', 100)])
    assert events == ['policies', 'impressions']


def test_failed_policy_write_does_not_record_an_unowned_impression(ledger):
    ledger.policies.publish.side_effect = OSError('disk full')
    with pytest.raises(OSError, match='disk full'):
        ledger.record_served_impressions('shown', 'home', [{'key': 'song', 'rank': 1}], 200,
                                         policies=[('version', '{}', 100)])
    ledger.impressions.record.assert_not_called()


def test_historical_report_uses_the_policy_owner_and_preserves_expired_status(ledger):
    ledger.impressions.facts.return_value = {
        'model_version': 'old', 'served_at': 100,
        'provenance': {'policy_kind': 'counterfactual', 'pool_policy': 'pool'}}
    ledger.policies.configuration.side_effect = [None, {'ranker': 'pool'}]
    ledger.policies.configuration_status.return_value = 'expired'
    result, attribution = ledger.served_recommendation_inputs(1)
    assert result['configuration'] is None
    assert result['configuration_status'] == 'expired'
    assert result['pool_configuration'] == {'ranker': 'pool'}
    assert attribution is None
    ledger.policies.configuration_status.assert_called_once_with('old')
    ledger.repository.scorecard_facts.assert_not_called()


def test_evidence_budget_follows_current_membership_and_projects_detached_facts(ledger):
    from yt_playlist.core.evidence_ledger import library_byte_budget
    ledger.repository.library_recording_count.return_value = 2000
    ledger.evidence()
    assert ledger.ledger_factory.call_args.args == (library_byte_budget(2000),)
    project = ledger.ledger_factory.call_args.kwargs['opportunity_projection']
    record = {'id': 'menu', 'context': 'old', 'format': 'albums'}
    ledger.repository.mix_projection_facts.return_value = {'installed': True, 'context': 'new'}
    assert project(record) == {**record, 'context': 'new'}


def test_unpublished_compact_menu_is_hidden_but_legacy_menu_remains_readable(ledger):
    from yt_playlist.core.mix_choice_policy import PRECISION
    ledger.repository.mix_projection_facts.return_value = {'installed': True, 'context': None}
    record = {'id': 'menu', 'context': 'old', 'format': 'mixes', 'options': []}
    assert ledger._mix_projection(record) == record
    assert ledger._mix_projection({**record, 'precision': PRECISION}) is None


def test_mix_validation_happens_before_any_sql_write(ledger):
    record = {'id': 'menu', 'at': 10, 'context': 'old', 'format': 'mixes',
              'options': [{'id': 'a', 'genres': ['']} ]}
    with pytest.raises(ValueError, match='genres'):
        ledger.publish_mix_menu(record, 'new')
    ledger.repository.publish_mix_menu.assert_not_called()


def test_mix_projection_requires_every_frozen_option_and_expands_only_display_values(ledger):
    record = {'id': 'menu', 'at': 10, 'context': 'old', 'format': 'mixes',
              'options': [{'id': 'a', 'label': 'House', 'genres': ['House'], 'tracks': [{'key': 'hidden'}]}]}
    ledger.repository.mix_projection_facts.return_value = {
        'installed': True, 'context': 'new', 'context_at': 10, 'options': {}}
    with pytest.raises(ValueError, match='incomplete'):
        ledger.publish_mix_menu(record, 'new')
    assert 'tracks' not in ledger.repository.publish_mix_menu.call_args.args[0]['options'][0]
    ledger.repository.mix_descriptor.return_value = {'genres': ['House'], 'label': 'House'}
    assert ledger.expand_mix_options([{'id': 'a', 'descriptor_id': 5}]) == [
        {'id': 'a', 'descriptor_id': 5, 'genres': ['House'], 'label': 'House'}]
