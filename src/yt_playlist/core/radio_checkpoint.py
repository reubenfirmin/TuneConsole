"""Decode the versioned Radio wire format without controller dependencies."""


def choice_key(choice):
    """Project the branch ID; menu attribution stays in the recovery document."""
    key = choice.get('id') if isinstance(choice, dict) else choice
    if key is not None and not isinstance(key, str):
        raise ValueError('Radio choice requires a text branch ID')
    return key


def project_choice(choice, key):
    """Apply the typed branch ID without discarding captured menu attribution."""
    return {**choice, 'id': key} if isinstance(choice, dict) else key


def normalize(state):
    state = dict(state)
    if state.get('version') == 1:
        if state.get('stopped'):
            phase = 'stopped'
        elif state.get('halted'):
            phase = 'conflict'
        elif state.get('initial_tracks'):
            phase = 'preparing'
        elif state.get('pending'):
            phase = 'appending'
        elif state.get('replacement') or state.get('requested_direction'):
            phase = 'replacing'
        else:
            phase = 'playing'
        state.update(version=2, phase=phase)
    if state.get('version') != 2:
        raise ValueError('Unsupported radio checkpoint version')
    state['stopped'] = state['phase'] in {'stopping', 'stopped'}
    state['halted'] = state['phase'] == 'conflict'
    return state
