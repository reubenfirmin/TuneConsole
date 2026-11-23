import pytest

from yt_playlist.core.rec_model_policy import PERMANENT_SURFACE
from yt_playlist.rec import choice_attribution, choice_evidence, choice_learning, measurement
from yt_playlist.rec import permanent_taste, rec_params, scoring


@pytest.mark.parametrize('format', ['albums', 'mixes', 'radio'])
@pytest.mark.parametrize('legacy_context', [False, True], ids=['ordinary', 'legacy-context'])
def test_confirmed_choice_uses_one_episode_in_taste(store, format, legacy_context):
    day = 86400
    at = 20 * day + 600
    account = store.accounts.upsert_account('listener', None, True)
    track = store.tracks.upsert_track('video', 'Song', 'Artist')
    store.tracks.set_track_genre(track, 'techno')
    key = store.tracks.identity_key_for_video('video')
    option = {'id': 'chosen', 'tracks': [{'video_id': 'video', 'key': key}], 'genres': ['techno']}
    context = {'comparison': {'run_id': 17, 'arm': 'incumbent'}} if legacy_context else {}
    observed = choice_evidence.issue(store, format, [option], at, context=context, episode_id='episode')
    unanswered = choice_evidence.issue(store, format, [option], at, context=context, episode_id='unanswered')
    for offer in (observed, unanswered):
        choice_evidence.respond(store, offer, 'visible', at + 1, visible=['chosen'])
    choice_evidence.compile_report(store, at + 2)
    assert choice_learning.contexts(store, at + 2) == []

    chosen_at, start_at = at + 3, at + 4
    choice_evidence.respond(store, observed, 'choice', chosen_at, option_id='chosen', action_id='deliberate')
    store.history.record_play_event(account, key, 'video', start_at, context_choice=True, source_timezone='UTC')
    measurement.record_action(store, 'start', start_at, keys=[key], event_id='same-play')
    for identity in ('start', 'duplicate-start'):
        choice_evidence.respond(store, observed, 'start', start_at, option_id='chosen', action_id=identity,
            details={'receipt_id': 'same-play', 'video_id': 'video', 'account_id': account})
    choice_evidence.respond(store, observed, 'experience', at + 100, option_id='chosen',
        details={'kind': 'ended', 'position': 96, 'duration': 96, 'start_at': start_at})
    choice_evidence.respond(store, observed, 'experience', at + 3600, option_id='chosen',
        action_id='follow-on', details={'video_id': 'automatic', 'continuation': True})
    choice_evidence.compile_report(store, at + 3601)
    assert len(store.history.play_events_since(0)) == 1
    assert store.history.play_events_since(0, taste_only=True) == []

    ledger = store.rec_ledger.evidence()
    offers = {offer['id']: offer for offer in ledger.records('opportunity')}
    assert 'comparison' not in offers[observed]
    responses = choice_attribution.observed_responses(ledger.records('response'), at + 3601)
    decision, = choice_attribution.actions(offers, responses, 6 * 3600)
    assert decision['choice']['id'] == 'deliberate'
    assert decision['choice']['source'] == 'live' and decision['choice']['precision'] == 'exact'
    assert decision['offer']['episode_id'] == 'episode'
    assert sum(row['kind'] == 'start' for row in decision['outcomes']) == 1

    for now, layer in ((at + 101, 'now'), (at + 3601, 'session'), (at + day, 'recent')):
        interest, = choice_learning.contexts(store, now)
        assert interest['choice_action_id'] == 'deliberate' and interest['at'] == chosen_at
        assert interest['days'] == interest['episodes'] == interest['episode_support'] == 1
        assert [name for name, weight in interest['layer_weights'].items() if weight > 0] == [layer]
    permanent_taste.prepare(store, at + 3601)
    baseline = store.rec_proposals.get_proposals(PERMANENT_SURFACE)
    assert baseline['facets']['genre:techno']['days'] == 1
    assert baseline['facets']['genre:techno']['support'] == 1
    assert baseline['weights'] == {}

def test_outcome_stays_with_original_choice_after_reselecting_the_same_option(store):
    oid = choice_evidence.issue(store, 'albums', [{'id': 'record'}], 100, context={})
    for identity, at in (('first', 101), ('second', 103)):
        choice_evidence.respond(store, oid, 'choice', at, option_id='record', action_id=identity)
    choice_evidence.respond(store, oid, 'start', 102, option_id='record', action_id='onset',
                           details={'choice_action_id': 'first'})
    choice_evidence.respond(store, oid, 'experience', 104, option_id='record', action_id='continuation',
                           details={'start_at': 102, 'video_id': 'next', 'continuation': True})
    choice_evidence.respond(store, oid, 'experience', 105, option_id='record', action_id='ending',
                           details={'start_at': 104, 'kind': 'ended'})
    ledger = store.rec_ledger.evidence()
    offers = {oid: ledger.get('opportunity', oid)}
    responses = choice_attribution.observed_responses(ledger.records('response'), 106)
    first, second = choice_attribution.actions(offers, responses, 100)
    assert [row['id'] for row in first['outcomes']] == ['onset', 'continuation', 'ending']
    assert second['outcomes'] == []

    choice_evidence.respond(store, oid, 'withdraw', 107, details={'action_id': 'first'})
    responses = choice_attribution.observed_responses(ledger.records('response'), 108)
    second, = choice_attribution.actions(offers, responses, 100)
    assert second['outcomes'] == []


@pytest.mark.parametrize('close_at_midnight', [False, True])
@pytest.mark.parametrize('format', ['albums', 'mixes'])
def test_verified_choices_share_graduation_support_with_organic_playback(store, close_at_midnight, format):
    day = 86400
    midnight = 20 * day
    account = store.accounts.upsert_account('listener', None, True)
    track = store.tracks.upsert_track('video', 'Song', 'Artist')
    store.tracks.set_track_genre(track, 'techno')
    key = store.tracks.identity_key_for_video('video')
    store.history.record_play_event(account, key, 'video', midnight - 600, source_timezone='UTC')
    if close_at_midnight:
        store.player_events.record_player_event(account, 'bye', 'video', 300, 300, None, '{}', midnight)

    option = {'id': 'chosen', 'tracks': [{'video_id': 'video', 'key': key}], 'genres': ['techno']}
    starts = (midnight - 300, midnight + 300, midnight + day + 300)
    for index, at in enumerate(starts):
        offer = choice_evidence.issue(store, format, [option], at - 3, context={})
        choice_evidence.respond(store, offer, 'visible', at - 2, visible=['chosen'])
        choice_evidence.respond(store, offer, 'choice', at - 1, option_id='chosen', action_id=f'choice-{index}')
        store.history.record_play_event(account, key, 'video', at, source_timezone='UTC', context_choice=True)
        choice_evidence.respond(store, offer, 'start', at, option_id='chosen',
            details={'video_id': 'video', 'account_id': account})

    now = starts[-1] + 5
    report = choice_evidence.compile_report(store, now)
    assert report['formats'][format]['starts'] == 3
    interest, = choice_learning.contexts(store, now)
    expected_units = 3 if close_at_midnight else 2
    assert interest['days'] == interest['episodes'] == expected_units
    assert interest['sustained'] == close_at_midnight
    assert bool(interest['layer_weights']['saved']) == close_at_midnight
    permanent_taste.prepare(store, now)
    baseline = store.rec_proposals.get_proposals(PERMANENT_SURFACE)
    assert baseline['facets']['genre:techno']['support'] == expected_units
    assert ('genre:techno' in baseline['weights']) == close_at_midnight
    if format == 'mixes' and close_at_midnight:
        axis = 'genre:techno'
        recent = scoring.facet_preferences(store, now + day)
        assert recent.recent[axis] > 0
        expired = now + (rec_params.get_param(store, 'recent_horizon_days') + 1) * day
        permanent_only = scoring.facet_preferences(store, expired)
        assert permanent_only.weights[axis] == baseline['weights'][axis] > 1
        assert axis not in permanent_only.recent
