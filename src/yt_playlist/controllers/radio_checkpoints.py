"""Order Radio catalog admission, checkpoint files and SQL publication."""
import json
import time

from yt_playlist.core import radio_checkpoint, radio_checkpoint_policy as policy, radio_policy_roots

SESSION_KEY = 'radio_current_session'
ORIGIN_KEYS = ('radio_session_playlist_ids', 'radio_playlist_ytm', 'radio_playlist_a_ytm', 'radio_playlist_b_ytm')


class RadioCheckpoints:
    def __init__(self, repository, settings, tracks, files, guard):
        self.repository, self.settings, self.tracks = repository, settings, tracks
        self.files, self.guard = files, guard

    def load(self):
        with self.files.retained():
            facts = self.repository.checkpoint_facts()
            if facts['native']:
                header = facts['header']
                if header is None:
                    return None, None
                state = self.files.read(self._reference(header), kind='radio-session', owner=header['id'])
                policy.project_queue(state, header, facts['queue'])
                revision = (header['id'], header['revision'])
                source = header['id']
            else:
                raw = self.files.read_prepared(facts['header']) if facts['prepared'] else self.settings.get_setting(SESSION_KEY)
                state, revision, source = json.loads(raw) if raw else None, None, SESSION_KEY
            if isinstance(state, dict):
                operation = self.repository.operation_facts(source, native=facts['native'])
                if operation is not None:
                    if operation['native']:
                        policy.project_operation(state, operation['header'], operation['roles'])
                    else:
                        header = operation['header']
                        captured = self.files.read(self._reference(header), kind='radio-session', owner=header['source'])
                        policy.project_prepared_operation(state, captured, header, operation['roles'])
            return state, revision

    def save(self, encoded, *, expected, predecessor=None):
        with self.guard(), self.files.retained():
            state = json.loads(encoded)
            owner, account = state.get('episode_id'), state.get('identity')
            facts = self.repository.save_facts(owner if isinstance(owner, str) else None,
                                               account if type(account) is int else None)
            if not facts['native']:
                self.settings.set_setting(SESSION_KEY, encoded)
                return None
            head = facts['head']
            observed = (head['id'], head['revision']) if head else None
            if expected is None and predecessor is not None and observed == (predecessor.get('episode_id'), 1):
                imported = self.files.read(self._reference(head), kind='radio-session', owner=head['id'])
                if radio_checkpoint.normalize(imported) == radio_checkpoint.normalize(predecessor):
                    expected = observed
            if observed != expected:
                raise ValueError('Radio session changed in another worker; reload its checkpoint')
            if not isinstance(owner, str) or not owner:
                raise ValueError('Radio checkpoint requires an episode ID')
            if type(account) is not int or not facts['account_exists']:
                raise ValueError('Radio checkpoint requires its recorded account')
            existing = facts['existing']
            if existing and (existing['account_id'] != account or not head or head['id'] != owner):
                raise ValueError('Radio episode identity cannot be reassigned or resurrected')
            revision = existing['revision'] + 1 if existing else 1
            phase = radio_checkpoint.normalize(state)['phase'] if state.get('version') == 1 else state.get('phase')
            queue = policy.queue(state)
            operation = policy.operation(state) if facts['operations'] else None
            versions = sorted(radio_policy_roots.queue_versions(state))
            entries = self.repository.queue_entries([track['video_id'] for track, _ in queue])
            for track, _ in queue:
                video = track['video_id']
                if video not in entries:
                    identity = self.tracks.observe_track_entry(video, track.get('title', ''), track.get('artist', ''),
                        preferred_key='@unresolved-video:' + video)
                    entries[video] = identity
            # Admission may reconcile aliases; read the final recording references.
            entries = self.repository.queue_entries([track['video_id'] for track, _ in queue])
            rows = [(position, *entries[track['video_id']], track['video_id'], status)
                    for position, (track, status) in enumerate(queue)]
            reference = self.files.publish('radio-session', owner, state)
            return self.repository.publish(state, phase, revision, reference, rows, operation, versions,
                                           expected=expected, now=time.time())

    def remember_playlists(self, playlists, account=None):
        playlists = list(playlists)
        if any(not isinstance(value, str) or not value for value in playlists):
            raise ValueError('Radio origin requires a provider playlist ID')
        with self.guard():
            facts = self.repository.origin_facts()
            if facts['native']:
                self.repository.remember_playlists(playlists, account)
            else:
                previous = self._origin_ids(facts)
                self.settings.set_setting(ORIGIN_KEYS[0], json.dumps(list(dict.fromkeys([*previous, *playlists]))))

    def playlist_ids(self):
        return self._origin_ids(self.repository.origin_facts())

    @staticmethod
    def _origin_ids(facts):
        if facts['ids'] is not None:
            return facts['ids']
        values = facts['values']
        return list(dict.fromkeys([*json.loads(values.get(ORIGIN_KEYS[0]) or '[]'),
            *(values[key] for key in ORIGIN_KEYS[1:] if values.get(key))]))

    @staticmethod
    def _reference(header):
        return tuple(header[key] for key in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))
