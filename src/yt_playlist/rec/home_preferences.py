"""Listening format defaults, separate from musical taste and manual overrides."""
import math

PANELS = {'albums': 'album-choices', 'mixes': 'directions', 'radio': 'radio'}
LABELS = {'albums': 'Long play', 'mixes': 'Mix tapes', 'radio': 'On air'}
MIN_LISTENING_DAYS = 3


def compile_usage(offers, responses, now, half_life_days):
    """One confirmed deliberate listen per format/episode/day; autoplay adds no votes.

    This runs with the existing choice worker. Home only reads its compact findings.
    A failed launch, visibility, opening a record and continued tracks cannot establish
    a preference. The existing choice memory control also governs this navigation hint.
    """
    choices, consumed, units, vibe_units = {}, set(), {}, set()
    for event in sorted(responses, key=lambda event: (event['at'], event['kind'] != 'choice', event['id'])):
        offer = offers.get(event['opportunity_id'])
        if not offer or offer['format'] not in PANELS or not offer['at'] <= event['at'] <= now:
            continue
        pair = (offer['id'], event.get('option_id'))
        if event['kind'] == 'choice':
            choices[pair] = event
        elif event['kind'] == 'start':
            choice = choices.get(pair)
            if not choice or choice['at'] > event['at'] or choice['id'] in consumed:
                continue
            consumed.add(choice['id'])
            unit = (offer['format'], offer['episode_id'], int(choice['at'] // 86400))
            units[unit] = max(units.get(unit, choice['at']), choice['at'])
            option = next((option for option in offer.get('options', [])
                           if str(option['id']) == event.get('option_id')), {})
            # Current compact menus keep the displayed preset in the decision
            # envelope. Older unprojected menus may still have the recipe.
            # Missing historical purpose cannot be inferred from today's tuning.
            if option and offer['format'] == 'mixes' and (
                    offer.get('vibe') or (option.get('recipe') or {}).get('vibe')):
                vibe_units.add(unit)
    scores = {format: 0. for format in PANELS}
    counts = {format: 0 for format in PANELS}
    days = {format: set() for format in PANELS}
    for (format, _, day), at in units.items():
        scores[format] += 2 ** (-(now - at) / (half_life_days * 86400))
        counts[format] += 1
        days[format].add(day)
    return {'scores': scores, 'episodes': counts,
            'vibe_episodes': len(vibe_units),
            'days': {format: len(value) for format, value in days.items()},
            'half_life_days': half_life_days}


def default_format(store):
    """An explicit Settings override wins; otherwise observed listening can evolve."""
    override = store.settings.get_setting('home_format')
    if override in PANELS:
        return override
    last = store.settings.get_setting('home_last_panel')
    if override == 'both':
        return next((format for format, panel in PANELS.items() if panel == last), 'mixes')
    usage = store.rec_proposals.choice_usage()
    scores, days = usage.get('scores') or {}, usage.get('days') or {}
    valid = {format: score for format, score in scores.items()
             if format in PANELS and type(score) in (int, float) and math.isfinite(score) and score > 0}
    if valid:
        best = max(valid.values())
        leaders = [format for format, score in valid.items() if math.isclose(score, best)]
        if len(leaders) == 1 and days.get(leaders[0], 0) >= MIN_LISTENING_DAYS:
            return leaders[0]
    stated = store.settings.get_setting('onboard_listening_format')
    if stated in PANELS:
        return stated
    return next((format for format, panel in PANELS.items() if panel == last), 'mixes')


def default_panel(store, *, albums_ready=False, mixes_ready=False):
    format = default_format(store)
    # Automatic Home should open the useful format that has arrived. An explicit
    # override keeps its meaning, including the existing last-tab setting.
    if store.settings.get_setting('home_format') not in {*PANELS, 'both'}:
        if format == 'mixes' and not mixes_ready and albums_ready:
            format = 'albums'
    return PANELS[format]
