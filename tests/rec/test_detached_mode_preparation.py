"""Mode and serving calculations run on supplied facts, with SQLite forbidden."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from tests.repository_mock_support import no_database
from yt_playlist.rec import autotune_run, home_audit, home_vibe, mode_eval, mode_profiles, mode_selection
from yt_playlist.rec import mode_surfaces, progressive, stored_groups, vibe_serving
from yt_playlist.util import genre_catalog

pytestmark = pytest.mark.usefixtures('no_database')


def test_bundle_membership_is_complete_while_temporal_preview_is_capped_per_band():
    keys = [f'key-{i}' for i in range(36)]
    vectors = np.array([[1., 0.] if i % 2 == 0 else [0., 1.] for i in range(36)], dtype=np.float32)
    modes = [{'mode_id': 1, 'centroid': np.array([1., 0.])},
             {'mode_id': 2, 'centroid': np.array([0., 1.])}]
    metadata = {key: {'title': key, 'artist': key, 'video_id': key} for key in keys}
    years = {key: 1980 + i for i, key in enumerate(keys)}
    pools = {lane: [] for lane in ('wheelhouse', 'explore', 'fresh', 'comfort')}
    payload = mode_surfaces.modeled_bundles(modes, (keys, vectors, {key: i for i, key in enumerate(keys)}),
        ([], None, {}), pools, generation='gen', space='space', cap=2,
        years=years, metadata=metadata, genres={}, suppressed={'key-0'})
    assert payload['_radio']['members'] == {'1': keys[::2], '2': keys[1::2]}
    all_temporal = {track['key'] for track in payload['all']['temporal']}
    assert all_temporal == set(keys) - {'key-0'}
    cuts = payload['_meta']['year_cuts']
    for mid in ('1', '2'):
        tracks = payload[mid]['temporal']
        assert len(tracks) == 6
        assert [sum(mode_surfaces._in_band(track['year'], band, *cuts) for track in tracks)
                for band in range(3)] == [2, 2, 2]
        assert {track['key'] for track in tracks} <= set(payload['_radio']['members'][mid])


def test_orphan_picks_do_not_create_posterior_evidence_and_repeat_picks_remain_bounded():
    modes = [{'mode_id': 1, 'label': 'One'}, {'mode_id': 2, 'label': 'Two'}]
    report = mode_eval.evidence_from_counts(modes, {2: 2}, {1: 8, 2: 9}, window_days=14)
    orphan, repeated = report['rows']
    assert (orphan['alpha'], orphan['beta'], orphan['has_evidence']) == (1, 1, False)
    assert (repeated['alpha'], repeated['beta'], repeated['has_evidence']) == (10, 1, True)
    assert repeated['estimate'] < 1 and report['picked'] == 17
    assert report == mode_eval.evidence_from_counts(modes, {2: 2}, {1: 8, 2: 9}, window_days=14)


def test_old_profile_retains_stored_counts_without_claiming_current_membership():
    mode = {'mode_id': 1, 'label': 'Old', 'families': [('jazz', 4)], 'size': 4, 'space': 'old'}
    profiles, current, _ = mode_profiles.profile_basis([mode], json.dumps({'cat': {}, 'ncat': 0, 'cont': []}),
                                                       {'jazz': 'Jazz'})
    assert not current
    assert profiles[1]['size'] == 4 and not profiles[1]['current']
    assert profiles[1]['eras'] == profiles[1]['sound'] == []


def test_assignment_keeps_the_only_mode_that_can_fill_fresh():
    lanes = ['wheelhouse', 'explore', 'fresh']
    depths = {(lane, mid): 12 for lane in lanes for mid in (1, 2, 3)}
    depths['fresh', 2] = depths['fresh', 3] = 0
    assigned = mode_selection.allocate_lanes([1, 2, 3], lanes, depths)
    assert assigned[-1] == 1 and set(assigned) == {1, 2, 3}


def test_full_card_requests_no_backfill():
    tracks = [{'key': f'k{i}', 'artist': f'a{i}', 'genre': 'Jazz'} for i in range(24)]
    bundles = {'1': {'wheelhouse': tracks, 'explore': tracks}, '_meta': {}}
    inputs = {'blocked': set(), 'muted': set()}
    steps = mode_surfaces.assembly_steps(inputs, bundles, [{'mode_id': 1}], [1], 0, 4, {},
        home_vibe.Ranker(None), cap_a=2, cap_al=2, ab_share=0, comfort_minimum=30)
    requests, response = [], None
    while True:
        try:
            request, value = steps.send(response)
        except StopIteration as completed:
            cards = completed.value
            break
        requests.append(request)
        assert request == 'decision', 'Full card unexpectedly requested metadata/backfill'
        response = {}
    assert len(cards) == 1 and len(cards[0]['tracks']) == 12
    assert requests == ['decision']


def test_group_page_rules_use_supplied_metadata_and_keep_temporal_band():
    page = [{'key': str(i), 'genre': 'Jazz', 'artist': f'a{i}', 'year': 1980 + i * 10}
            for i in range(5)]
    result = list(stored_groups.eligible_page(page, 'temporal', 1, ('jazz',), (), set(),
        {'year_cuts': [1990, 2010]}, blocked={'2'}, axes={'Jazz': ('jazz', None)}, muted={'a0'},
        weights={}, maximum=600))
    assert [item['key'] for item in result] == ['3']


def test_preview_caps_continue_across_detached_pages():
    frame = stored_groups.PreviewFrame()
    stored_groups.preview_page([{'key': '1', 'artist': 'A', 'album': 'X'}], set(), set(), 2, 1, frame)
    assert frame.count == 1
    stored_groups.preview_page([{'key': '2', 'artist': 'A', 'album': 'X'},
        {'key': '3', 'artist': 'A', 'album': 'Y'}, {'key': '4', 'artist': 'A', 'album': 'Z'}],
        set(), set(), 2, 1, frame)
    assert frame.count == 2 and frame.available == 4
    assert [track['key'] for track in frame.tracks] == ['1', '3']


def test_basis_and_full_fit_admission_keep_distinct_change_floors_and_age_escape():
    previous = {'rows': 10, 'at': 100, 'config': []}
    facts = dict(changed=1, has_model=True, has_evidence=True, coherent=True,
                 genre_revision=genre_catalog.REVISION, now=101)
    assert progressive.basis_reason(previous, **facts) == 'accumulated-evidence'
    assert not progressive.full_due_values(previous, [], True, True, 1, 0, 101)
    assert progressive.full_due_values(previous, [], True, True, 1, 0, 1000)
    assert progressive.basis_reason(previous, **{**facts, 'has_model': False,
                                                'has_evidence': False}) is None


def test_report_projections_preserve_exact_identity_and_unknown_coverage():
    cards = [{'tracks': [{'key': 'exact-upload'}], 'pool_lane': 'fresh'}]
    assert vibe_serving.indexed_cards(cards, [[{'key': 'other-version'}]]) is None
    hydrated = vibe_serving.indexed_cards(cards, [[{'key': 'exact-upload', 'video_id': 'exact-video'}]])
    assert hydrated[0]['tracks'][0]['video_id'] == 'exact-video'
    proto = {'tracks': [{'key': 'unknown'}], 'lane': 'fresh', 'label': 'Fresh', 'note': 'New songs'}
    audit = home_audit.projection([proto], {'artists': {}, 'decades': {}}, 0,
                                  source='home', epoch=3, card_epochs={'fresh': 7})
    assert audit['cards'][0]['unknown_genre_count'] == audit['cards'][0]['unknown_decade_count'] == 1
    assert audit['cards'][0]['epoch'] == 7
    assert autotune_run.decode_report('{"before": 1}') == {'before': 1}
    for raw in (None, 'invalid', '[]', '1'):
        assert autotune_run.decode_report(raw) is None
