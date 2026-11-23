"""Publish a confirmed Radio destination, its Undo, and its first checkpoint."""
import json
import time
from dataclasses import dataclass

from yt_playlist.core import radio_checkpoint_policy as policy, radio_policy_roots
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.library.playlist_metadata import GENERATED_DESCRIPTION, content_hash
from yt_playlist.repos.base import GENERATED_GROUP


@dataclass(frozen=True)
class RadioLaunchPublication:
    params: str
    undo: str
    group: str

    @classmethod
    def for_seeded(cls, title, account, playlist, post):
        videos = post['videos']
        params = {'title': title, 'source': 'recommendations', 'group': GENERATED_GROUP,
                  'added': len(videos), 'skipped': 0, 'partial': False,
                  'added_handles': dict(zip(videos, post['handles'])),
                  'addition': {'confirmed': videos, 'rejected': [], 'uncertain': [], 'unattempted': []}}
        return cls(json.dumps(params), json.dumps({'new_ytm': playlist, 'target_identity': account, 'post': post}),
                   GENERATED_GROUP)

    def save(self, store, checkpoints, encoded, *, expected, now):
        state = json.loads(encoded)
        owner, account, remote = state['episode_id'], state['identity'], state['playlist']
        queue = policy.queue(state)
        videos = list(dict.fromkeys(track['video_id'] for track, _ in queue))
        proof = json.loads(self.undo)
        if (not videos or policy.operation(state) is not None
                or (remote, account, videos) != (proof['new_ytm'], proof['target_identity'], proof['post']['videos'])):
            raise ValueError('Initial Radio publication requires a confirmed starting queue')
        versions = sorted(radio_policy_roots.queue_versions(state))
        actions = store.actions
        repo = checkpoints.repository
        with actions.files.retained(), checkpoints.files.retained():
            integrity = checkpoints.files.describe('radio-session', owner, state)
            with atomic_write(store):
                action = actions.reserve_radio_copy(self.params, self.undo, now)
                file_id = repo.reserve_file(owner, integrity)
            action = actions.write_radio_copy(action)
            reference = checkpoints.files.publish_reserved(file_id, 'radio-session', owner, state, expected=integrity)
            with atomic_write(store):
                entries = repo.launch_entries(videos)
                missing = [track for track, _ in queue if track['video_id'] not in entries]
                for track in missing:
                    store.tracks.upsert_track(track['video_id'], track.get('title') or '', track.get('artist') or '',
                        track.get('album') or '', track.get('duration'), thumbnail=track.get('thumbnail'))
                if missing:
                    entries = repo.launch_entries(videos)
                rows = [(position, *entries[track['video_id']][:2], track['video_id'], status)
                        for position, (track, status) in enumerate(queue)]
                keys = {entries[video][2] for video in videos}
                thumbnail = next((track['thumbnail'] for track, _ in queue if track.get('thumbnail')), None)
                pid, scope = store.playlists.repository.publish_radio_destination(
                    account, remote, state['title'], self.group, videos, content_hash(keys), thumbnail,
                    proof['post'].get('description') or GENERATED_DESCRIPTION, now)
                repo.remember_scope(scope)
                actions.repository.publish_radio_copy(action, scope=scope, account=account)
                revision = repo.publish(state, state['phase'], 1, reference, rows, None, versions,
                    file_id=file_id, expected=expected, now=time.time(), new_scope=scope)
        return pid, revision
