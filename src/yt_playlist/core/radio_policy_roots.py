"""Explicit serving-policy custody for saved Radio queues and seed offers."""
from . import home_outputs


def queue_versions(state):
    tracks = [*state.get('tracks', []), *state.get('initial_tracks', [])]
    if state.get('pending'):
        tracks.append(state['pending'])
    return home_outputs.policy_references({'tracks': tracks})


def save_session(conn, identity, state):
    if not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='radio_session_policies'").fetchone():
        return
    conn.execute('DELETE FROM radio_session_policies WHERE session_id=?', (identity,))
    for version in sorted(queue_versions(state)):
        known = conn.execute('SELECT 1 FROM recommendation_policies WHERE version=?', (version,)).fetchone()
        conn.execute('INSERT INTO radio_session_policies VALUES (?,?,?,?)',
                     (identity, version, version if known else None, None if known else 'missing_policy'))
