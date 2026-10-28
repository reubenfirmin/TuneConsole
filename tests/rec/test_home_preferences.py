import pytest

from yt_playlist.rec import choice_evidence, home_preferences


def listen(store, format, day, episode):
    at = day * 86400 + 100
    oid = choice_evidence.issue(store, format, [{'id': 'option'}], at,
        context={}, opportunity_id=f'{format}-{day}-{episode}', episode_id=episode)
    choice_evidence.respond(store, oid, 'choice', at + 1, option_id='option')
    choice_evidence.respond(store, oid, 'start', at + 2, option_id='option')
    return oid


def test_automatic_home_needs_confirmed_listening_on_three_distinct_days(store):
    store.settings.set_setting('onboard_listening_format', 'albums')
    for episode in ('morning', 'afternoon', 'evening'):
        listen(store, 'radio', 1, episode)
    report = choice_evidence.compile_report(store, 2 * 86400)
    assert report['home_format']['episodes']['radio'] == 3
    assert report['home_format']['days']['radio'] == 1
    assert home_preferences.default_format(store) == 'albums'
    # Multiple days in the same radio session still establish deliberate repeat use.
    listen(store, 'radio', 2, 'morning')
    choice_evidence.compile_report(store, 3 * 86400)
    assert home_preferences.default_format(store) == 'albums'
    listen(store, 'radio', 3, 'morning')
    choice_evidence.compile_report(store, 4 * 86400)
    assert home_preferences.default_format(store) == 'radio'
    store.settings.set_setting('home_format', 'mixes')
    assert home_preferences.default_format(store) == 'mixes'
    store.settings.set_setting('home_format', 'both')
    store.settings.set_setting('home_last_panel', 'album-choices')
    assert home_preferences.default_format(store) == 'albums'


def test_passive_autoplay_failed_launches_and_withdrawn_choices_cannot_switch_home(store):
    store.settings.set_setting('onboard_listening_format', 'albums')
    for day in (1, 2, 3):
        at = day * 86400 + 100
        oid = choice_evidence.issue(store, 'mixes', [{'id': 'option'}], at,
            context={}, opportunity_id=f'mix-{day}')
        for kind in ('visible', 'open', 'choice'):
            choice_evidence.respond(store, oid, kind, at + 1, option_id='option', visible=['option'])
        radio = choice_evidence.issue(store, 'radio', [{'id': 'option'}], at,
            context={}, opportunity_id=f'radio-{day}')
        choice_evidence.respond(store, radio, 'start', at + 2, option_id='option')
        withdrawn = listen(store, 'radio', day, 'withdrawn')
        choice_evidence.respond(store, withdrawn, 'withdraw', at + 3,
            details={'action_id': f'{withdrawn}:choice:option'})
    usage = choice_evidence.compile_report(store, 4 * 86400)['home_format']
    assert all(days == 0 for days in usage['days'].values())
    assert home_preferences.default_format(store) == 'albums'


def test_older_reports_and_ties_keep_the_stated_preference(store):
    store.settings.set_setting('onboard_listening_format', 'albums')
    store.rec_proposals.put_proposals('listening_choices', {'home_format': {
        'scores': {'mixes': 10}}}, 100)
    assert home_preferences.default_format(store) == 'albums'
    store.rec_proposals.put_proposals('listening_choices', {'home_format': {
        'scores': {'mixes': 10, 'radio': 10}, 'days': {'mixes': 3, 'radio': 3}}}, 100)
    assert home_preferences.default_format(store) == 'albums'


@pytest.mark.parametrize('override', ['albums', 'mixes', 'radio', 'both', 'auto'])
def test_explicit_settings_choice_resolves_an_old_pending_picker(store, override):
    store.settings.set_setting('onboard_format_pending', '1')
    assert home_preferences.choice_pending(store)
    store.settings.set_setting('home_format', override)
    assert not home_preferences.choice_pending(store)
