"""Direct, store-free coverage of growth_access's detached decisions.

Every function here takes plain supplied values -- no Store, reader,
Request or Ctx is constructed anywhere in this file.
"""
import pytest

from yt_playlist.rec import growth_access


def _state(*, vibes_ready=False, ready_areas=None, nodes=()):
    return {'vibes_ready': vibes_ready, 'ready_areas': ready_areas or {}, 'nodes': nodes}


def test_has_access_vibes_prefers_the_ready_flag_over_node_lookup():
    assert growth_access.has_access(_state(vibes_ready=True), 'vibes') is True


def test_has_access_vibes_falls_back_to_an_unlocked_vibes_node():
    nodes = ({'id': 'vibes', 'unlocked': True}, {'id': 'tv', 'unlocked': False})
    assert growth_access.has_access(_state(nodes=nodes), 'vibes') is True
    assert growth_access.has_access(_state(nodes=({'id': 'vibes', 'unlocked': False},)), 'vibes') is False


def test_has_access_other_area_prefers_ready_areas_then_unlocked_href():
    assert growth_access.has_access(_state(ready_areas={'tv': True}), 'tv') is True
    nodes = ({'id': 'tv', 'unlocked': True, 'href': '/#tv'},)
    assert growth_access.has_access(_state(ready_areas={'tv': False}, nodes=nodes), 'tv') is True
    locked = ({'id': 'tv', 'unlocked': False, 'href': '/#tv'},)
    assert growth_access.has_access(_state(ready_areas={'tv': False}, nodes=locked), 'tv') is False


@pytest.mark.parametrize('area,used,expected', [
    ('directions', 'listening', True), ('album-choices', 'albums', True),
    ('radio', 'radio', True), ('vibes', 'vibes', True), ('tv', 'tv', True),
    ('discover', 'listening', True),  # discover precedes listening on MAIN_PATH
    ('album-choices', 'radio', True),  # reaching a later stop implies every earlier one too
    ('radio', 'albums', False),  # albums precedes radio: an earlier stop alone doesn't grant it
])
def test_retained_access_matches_the_main_path_prefix(area, used, expected):
    usage = {identity: identity == used for identity in ('listening', 'albums', 'radio', 'vibes', 'tv')}
    assert growth_access.retained_access(usage, area) is expected


def test_retained_access_is_false_for_an_unmapped_area_without_inspecting_usage():
    usage = {'listening': True, 'albums': True, 'radio': True, 'vibes': True, 'tv': True}
    assert growth_access.retained_access(usage, 'setup') is False


def test_retained_access_is_false_with_no_usage_at_all():
    assert growth_access.retained_access({}, 'radio') is False


def test_vibe_progression_preserves_earned_access_without_current_preparation():
    assert growth_access.vibe_progression(vibe_episodes=True, resume=[], mixes=False,
        vibes_eligible=False, usable={}) is True


def test_vibe_progression_denies_without_mixes():
    assert growth_access.vibe_progression(vibe_episodes=False, resume=[], mixes=False,
        vibes_eligible=True, usable={}) is False


def test_vibe_progression_denies_when_not_eligible():
    assert growth_access.vibe_progression(vibe_episodes=False, resume=[], mixes=True,
        vibes_eligible=False, usable={}) is False


def test_vibe_progression_only_checks_stops_after_the_historical_resume_point():
    assert growth_access.vibe_progression(vibe_episodes=False, resume=['albums'], mixes=True,
        vibes_eligible=True, usable=dict.fromkeys(('discover', 'listening', 'radio', 'tv'), True)) is True
    assert growth_access.required_prefix(['albums']) == ('discover', 'listening', 'radio', 'tv')


def test_vibe_progression_denies_on_the_first_unusable_predecessor_without_checking_later_ones():
    assert growth_access.vibe_progression(vibe_episodes=False, resume=[], mixes=True,
        vibes_eligible=True, usable=dict.fromkeys(('first', 'foundation', 'albums'), True)) is False


def test_vibe_progression_grants_when_resume_already_covers_every_predecessor():
    assert growth_access.vibe_progression(vibe_episodes=False, resume=['tv'], mixes=True,
        vibes_eligible=True, usable={}) is True
