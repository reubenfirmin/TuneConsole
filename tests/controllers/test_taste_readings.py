"""Taste presentation captures profiles without changing choice evidence."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from tests.repository_mock_support import no_database
from yt_playlist.controllers.taste_readings import TasteReadings
from yt_playlist.rec.mode_eval import evidence_from_counts

pytestmark = pytest.mark.usefixtures('no_database')


@pytest.mark.parametrize('offers,picks', [({}, {}), ({1: 2, 2: 2}, {1: 1}),
                                        ({1: 100, 2: 100, 3: 100}, {1: 30, 3: 4})])
def test_choice_summaries_keep_real_profiles_and_unchanged_evidence(offers, picks):
    modes = [{'mode_id': mid, 'label': f'Legacy {mid}'} for mid in range(1, 4)]
    evidence = evidence_from_counts(modes, offers, picks, window_days=30)
    before = deepcopy(evidence)
    profiles = {mid: {'title': f'Theme {mid}', 'genres': [{'name': 'Jazz', 'share': .75}]}
                for mid in range(1, 4)}
    analysis = Mock(spec_set=['group_prepared', 'choice_evidence', 'mode_profiles'])
    analysis.group_prepared.return_value = None
    analysis.choice_evidence.return_value = evidence
    analysis.mode_profiles.return_value = profiles
    readings = TasteReadings(queries=None, analysis=analysis, parameter=None, configuration=None)

    result = readings.mix_choices(1000)

    sections = [('rows', result['rows'], evidence['rows']),
                ('strongest', result['strongest'], evidence['strongest'])]
    if evidence['contrast'] is None:
        assert result['contrast'] is None
    else:
        sections.extend((side, result['contrast'][side], evidence['contrast'][side])
                        for side in ('favored', 'less_supported'))
    for _section, actual, expected in sections:
        assert [{key: value for key, value in row.items() if key != 'profile'}
                for row in actual] == expected
        assert all(row['profile'] is profiles[row['mode_id']] for row in actual)
    assert (result['picked'], result['offered'], result['window_days']) == (
        evidence['picked'], evidence['offered'], 30)
    assert evidence == before
    analysis.choice_evidence.assert_called_once_with(1000)
    analysis.mode_profiles.assert_called_once_with()
