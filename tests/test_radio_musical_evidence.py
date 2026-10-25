"""Visible radio offers retain the actual intent, scorer and cached musical inputs."""
from copy import deepcopy

from tests.radio_session_support import poc  # noqa: F401 - shared fixture
from yt_playlist.rec import musical_character, musical_profiles
from yt_playlist.rec.radio_candidates import POLICY_VERSION


def test_seed_and_direction_measurement_preserve_profile_provenance_and_unknowns(poc, monkeypatch):
    from yt_playlist.rec import choice_evidence, measurement
    from yt_playlist.web import radio_session
    controller, _remote, _frames, _workers, pool = poc
    profile = musical_character.compact(musical_profiles.resolve([], overrides={'energy': .9}))
    for track in pool[::2]:
        track['musical'] = deepcopy(profile)
    offers, menus = {}, []
    def record(store, surface, items, now, **kwargs):
        offers[surface] = deepcopy(items), deepcopy(kwargs)
    def menu(store, format, options, now, **kwargs):
        menus.append(deepcopy(kwargs['context']))
    monkeypatch.setattr(measurement, 'record_recommendations', record)
    monkeypatch.setattr(choice_evidence, 'safe_issue', menu)
    candidates = radio_session.RadioCandidates(controller.ctx.store, 1234,
        {**radio_session.radio_tuning.DEFAULTS, 'energy': .9})
    controller.listening.seed_offer(candidates)
    assert menus[-1]['radio']['tuning']['energy'] == .9
    assert menus[-1]['radio']['ordering_ranker'] == POLICY_VERSION
    assert controller.start()['ok']
    session = controller.session
    controller.listening.seen(session, session.directions.menu_id)
    assert menus[-1]['radio']['musical_policy'] == musical_character.VERSION
    for surface in ('radio_seed', 'radio_directions'):
        items, _options = offers[surface]
        assert items
        for item in items:
            context = item['serving_context']
            assert context['ordering_ranker'] == POLICY_VERSION
            assert context['musical_policy'] == musical_character.VERSION
            if item.get('musical'):
                assert context['musical_version'] == profile['version']
                assert context['musical_digest'] == profile['digest']
                assert context['intensity'] == .9
            else:
                assert context['musical_version'] is None and context['musical_digest'] is None
                assert context['intensity'] is None and context['intensity_support'] == 0
