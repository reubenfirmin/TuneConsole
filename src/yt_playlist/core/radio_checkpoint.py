"""Decode the versioned Radio wire format without controller dependencies."""


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
