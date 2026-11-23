"""Explicit serving-policy custody for saved Radio queues and seed offers."""
import json

from . import home_outputs
from .recommendation_outputs import RADIO_SEED_SETTING


SESSION_VERSIONS = 'SELECT r.key FROM radio_sessions o,json_each(o.policy_roots) r WHERE r.value=1'
VERSIONS = (SESSION_VERSIONS + " UNION SELECT r.key FROM settings s,json_each(s.value,'$.policy_roots') r "
            f"WHERE s.key='{RADIO_SEED_SETTING}' AND r.value=1")


def queue_versions(state):
    tracks = [*state.get('tracks', []), *state.get('initial_tracks', [])]
    if state.get('pending'):
        tracks.append(state['pending'])
    return home_outputs.policy_references({'tracks': tracks})


def encode(conn, versions):
    return conn.execute('SELECT json_group_object(v.value,p.version IS NOT NULL) '
                        'FROM json_each(?) v LEFT JOIN recommendation_policies p ON p.version=v.value',
                        (json.dumps(sorted(set(versions))),)).fetchone()[0]


def save_session(conn, identity, versions):
    roots = encode(conn, versions)
    conn.execute('UPDATE radio_sessions SET policy_roots=? WHERE id=? AND policy_roots<>?', (roots, identity, roots))
