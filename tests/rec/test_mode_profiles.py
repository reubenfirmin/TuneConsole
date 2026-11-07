"""Mode description arithmetic uses captured repository inputs, without SQLite."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from yt_playlist.rec import content_model, mode_profiles


def profile_inputs():
    model = {'cat': {'fam:house': 0, 'dec:2010': 1, 'dec:2020': 2},
             'ncat': 3, 'cont': [['bpm', 120, 10], ['energy', .5, .2]]}
    space = content_model.content_model_fingerprint(model)
    centers = np.eye(5, dtype=np.float32)[:2]
    modes, vectors, content, audio = [], [], {}, {}
    for group in range(2):
        keys = []
        for i in range(10):
            key = f'group {group} track {i}|artist'
            genre = 'House' if group or i < 4 else 'Funk' if i < 7 else 'Drum And Bass'
            keys.append(key)
            content[key] = genre, 2023 if group else 2013
            vectors.append((key, centers[group].tobytes()))
            if group and i < 8:
                audio[key] = {'bpm': 128, 'energy': .9}
        modes.append({'mode_id': group + 1, 'label': 'house', 'size': 20,
                      'families': [['house', 4]], 'centroid': centers[group],
                      'rep_keys': keys[:2], 'space': space})
    axes = {'House': ('house', 'House'), 'Funk': ('soul-funk', 'Funk'),
            'Drum And Bass': ('dnb', 'Drum And Bass'), '': ('', None)}
    return SimpleNamespace(
        modes=Mock(list_modes=Mock(return_value=modes)),
        rec_model=Mock(content_generation=Mock(return_value={'model': json.dumps(model), 'library': vectors})),
        rec_query=Mock(track_content=Mock(return_value=content), track_audio_features=Mock(return_value=audio)),
        genres=Mock(feature_axes=Mock(side_effect=lambda labels: {label: axes[label] for label in labels})))


def test_profiles_explain_same_label_groups_using_actual_membership():
    reader = profile_inputs()
    profiles = mode_profiles.describe_modes(reader)
    mixed, house = profiles[1], profiles[2]
    assert mixed['current'] and house['current']
    assert mixed['title'] == '2010s blend'
    assert house['title'] == 'House · 2020s'
    assert mixed['size'] == house['size'] == 10
    assert [(g['name'], g['share']) for g in mixed['genres']] == [
        ('House', .4), ('Soul & Funk', .3), ('Drum & Bass', .3)]
    assert house['genres'][0]['share'] == 1
    assert mixed['eras'] == [{'name': '2010s', 'count': 10, 'share': 1}]
    assert mixed['sound'] == []
    assert house['sound'] == [
        {'text': '128 BPM median', 'measured': 8, 'total': 10},
        {'text': 'Higher energy', 'measured': 8, 'total': 10}]


def test_profiles_preserve_missing_tags_and_dates_in_the_denominator():
    reader = profile_inputs()
    reader.rec_query.track_content.return_value['group 0 track 0|artist'] = '', ''
    mixed = mode_profiles.describe_modes(reader)[1]
    assert mixed['size'] == 10
    assert sum(g['share'] for g in mixed['genres']) == pytest.approx(1)
    assert next(g for g in mixed['genres'] if g['name'] == 'House')['share'] == .3
    assert mixed['dated'] == 9
    assert mixed['eras'][0]['share'] == .9


def test_stale_profiles_do_not_compare_incompatible_vectors_or_guess_traits():
    reader = profile_inputs()
    reader.rec_model.content_generation.return_value['model'] = json.dumps({
        'cat': {'fam:house': 0, 'dec:2010': 1, 'dec:2020': 2, 'sub:house': 3},
        'ncat': 4, 'cont': [['bpm', 120, 10], ['energy', .5, .2]]})
    profile = mode_profiles.describe_modes(reader)[1]
    assert not profile['current']
    assert profile['size'] == 20
    assert profile['genres'][0]['share'] == .2
    assert profile['genres'][1]['share'] == .8
    assert profile['eras'] == profile['sound'] == []
    reader.rec_query.track_content.assert_not_called()
    reader.rec_query.track_audio_features.assert_not_called()
