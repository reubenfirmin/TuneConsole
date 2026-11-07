"""Learning output decisions require no database or shared transaction."""
from unittest.mock import Mock, call

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.choice_findings import ChoiceFindings

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.mark.parametrize('previous,changed', [
    (None, True), ({'at': 1, 'watermark': 'old', 'weights': [1]}, False),
    ({'at': 1, 'watermark': 'old', 'weights': [2]}, True),
])
def test_publication_updates_interests_only_when_effective_values_change(previous, changed):
    outputs = Mock()
    outputs.get_proposals.return_value = previous
    findings = ChoiceFindings(outputs)
    interests = {'at': 2, 'watermark': 'new', 'weights': [1]}
    findings.publish({'report': 1}, interests, 2, bandit={'bandit': 1}, group_bandit={'groups': 1})
    expected = [call('listening_choices', {'report': 1}, 2), call('choice_bandit', {'bandit': 1}, 2)]
    if changed:
        expected.append(call('choice_interests', interests, 2))
    expected.append(call('group_choice_bandit', {'groups': 1}, 2))
    assert outputs.put_proposals.call_args_list == expected


def test_failed_output_stops_following_publications_without_replaying_prior_writes():
    outputs = Mock()
    outputs.get_proposals.return_value = None
    outputs.put_proposals.side_effect = [None, OSError('publication interrupted')]
    with pytest.raises(OSError, match='publication interrupted'):
        ChoiceFindings(outputs).publish({}, {}, 2, bandit={}, group_bandit={})
    assert outputs.put_proposals.call_args_list == [
        call('listening_choices', {}, 2), call('choice_bandit', {}, 2)]
