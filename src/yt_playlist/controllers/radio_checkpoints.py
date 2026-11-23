"""Order Radio catalog admission, checkpoint files and SQL publication."""
import json
import time

from yt_playlist.core import radio_checkpoint, radio_checkpoint_policy as policy, radio_policy_roots

SESSION_KEY = 'radio_current_session'
ORIGIN_KEYS = ('radio_session_playlist_ids', 'radio_playlist_ytm', 'radio_playlist_a_ytm', 'radio_playlist_b_ytm')


class RadioCheckpoints:
    def __init__(self, repository, tracks, files, guard):
        self.repository, self.tracks = repository, tracks
        self.files, self.guard = files, guard

    def launch_receipts(self):
        return self.repository.launch_receipts()

    def first_sounds(self):
        return self.repository.first_sounds()

    def save_first_sounds(self, state, *, new=False):
        self.repository.save_first_sounds(state, new=new)

    def reserve_launch(self, launch_id, episode, *, predecessor=None, clear_seeded=False, seeded_recovery=None):
        return self.repository.reserve_launch(launch_id, episode, predecessor=predecessor,
                                              clear_seeded=clear_seeded, seeded_recovery=seeded_recovery)

    def pending_seeded_launch(self):
        return self.repository.pending_seeded_launch()

    def update_seeded_dispatch(self, launch_id, playlist, *, started, was_started=False):
        return self.repository.update_seeded_dispatch(launch_id, playlist, started=started, was_started=was_started)

    def take_seeded_launch(self, launch_id):
        return self.repository.take_seeded_launch(launch_id)

    def load(self):
        with self.files.retained():
            facts = self.repository.checkpoint_facts()
            header = facts['header']
            if header is None:
                return None, None
            state = self.files.read(self._reference(header), kind='radio-session', owner=header['id'])
            policy.project_queue(state, header, facts['queue'])
            if state.get('local_id') is None and header.get('local_playlist_id') is not None:
                state['local_id'] = header['local_playlist_id']
            revision = (header['id'], header['revision'])
            policy.restore_operation(state, header['operation_json'])
            return state, revision

    def save(self, encoded, *, expected, predecessor=None):
        with self.guard(), self.files.retained():
            state = json.loads(encoded)
            owner, account = state.get('episode_id'), state.get('identity')
            facts = self.repository.save_facts(owner if isinstance(owner, str) else None,
                                               account if type(account) is int else None)
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
            operation = policy.operation(state)
            versions = sorted(radio_policy_roots.queue_versions(state))
            videos = [track['video_id'] for track, _ in queue]
            entries = self.repository.queue_entries(videos)
            missing = {track['video_id']: track for track, _ in queue if track['video_id'] not in entries}
            for video, track in missing.items():
                self.tracks.observe_track_entry(video, track.get('title', ''), track.get('artist', ''),
                    preferred_key='@unresolved-video:' + video)
            if missing:
                # Admission may reconcile aliases; read the final recording references.
                entries = self.repository.queue_entries(videos)
            rows = [(position, *entries[track['video_id']], track['video_id'], status)
                    for position, (track, status) in enumerate(queue)]
            integrity = self.files.describe('radio-session', owner, state)
            file_id = self.repository.reserve_file(owner, integrity)
            reference = self.files.publish_reserved(file_id, 'radio-session', owner, state, expected=integrity)
            return self.repository.publish(state, phase, revision, reference, rows, operation, versions,
                                           file_id=file_id, expected=expected, now=time.time())

    def remember_playlists(self, playlists, account=None):
        playlists = list(playlists)
        if any(not isinstance(value, str) or not value for value in playlists):
            raise ValueError('Radio origin requires a provider playlist ID')
        with self.guard():
            self.repository.remember_playlists(playlists, account)

    def playlist_ids(self):
        return self.repository.playlist_ids()

    @staticmethod
    def _reference(header):
        return tuple(header[key] for key in ('recovery_filename', 'recovery_digest', 'recovery_bytes'))
