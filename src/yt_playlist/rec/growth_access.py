"""Detached progression/access policies, callable without Store, Request or Ctx.

`account_growth.py` (which still re-exports these) owns acquiring the facts these
functions decide over; these never touch a reader, repository or provider.
"""

MAIN_PATH = ('first', 'foundation', 'albums', 'discover', 'listening', 'radio', 'tv', 'vibes', 'recent')

RETAINED_DESTINATIONS = {'directions': 'listening', 'album-choices': 'albums',
                         'radio': 'radio', 'tv': 'tv', 'discover': 'discover',
                         'vibes': 'vibes', 'recent': 'recent'}


def has_access(state, area):
    """Keep earned destinations open while their eligible output refreshes.

    Readiness still describes usable output and controls new-user progression.
    Historical use can retain access, but never asserts that output is ready.
    """
    if area == 'vibes':
        return state['vibes_ready'] or any(node['id'] == 'vibes' and node['unlocked']
                                         for node in state['nodes'])
    return state['ready_areas'].get(area, False) or any(
        node['unlocked'] and node['href'] == '/#'+area for node in state['nodes'])


def retained_access(usage, area):
    """Has `area` already been earned by deliberate prior listening?

    `usage` is already-acquired {'listening', 'albums', 'radio', 'vibes', 'tv': bool}
    facts, matching the historical prefix account_growth._connect_paths would resume.
    """
    destination = RETAINED_DESTINATIONS.get(area)
    if destination is None:
        return False
    resume = {identity for identity in ('listening', 'albums', 'radio', 'vibes', 'tv') if usage.get(identity)}
    return any(MAIN_PATH.index(destination) <= MAIN_PATH.index(identity) for identity in resume)


def required_prefix(resume, destination='vibes'):
    historical = max((MAIN_PATH.index(identity) for identity in resume), default=-1)
    return MAIN_PATH[historical+1:MAIN_PATH.index(destination)]


def vibe_progression(*, vibe_episodes, resume, mixes, vibes_eligible, usable):
    """Has Vibes been earned, given already-acquired progression facts?

    `resume` contains destinations with deliberate prior use. `usable` contains
    acquired readiness booleans for the prefix after that historical resume point.
    """
    if vibe_episodes:
        return True
    if not mixes or not vibes_eligible:
        return False
    return all(usable.get(identity, False) for identity in required_prefix(resume))
