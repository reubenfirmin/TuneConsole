"""Resume bounded candidate frames separately from report-based allocation."""
from yt_playlist.core.activity import check_stopping
from yt_playlist.rec.group_preview_values import candidate_frame
from yt_playlist.rec.stored_groups import PreviewFrame


class GroupCandidates:
    def __init__(self, *, capture, frame):
        self.capture, self.frame = capture, frame

    def prepare(self, head, now, *, progress=None, checkpoint=None):
        progress = progress if progress is not None else {}
        completed = progress.setdefault('frames', {})
        facts = self.capture(now)
        typed, generation, pools = facts['typed'], facts['generation'], facts['pools']
        prepared = {}
        for group in head['groups']:
            if group.get('legacy'):
                continue
            check_stopping()
            members, frames = set(group.get('members', {})), {}
            for frame, pool in pools.items():
                frame_key = (group['version'], frame)
                if typed:
                    state = completed.setdefault(frame_key, PreviewFrame())
                    frames[frame] = self.frame(generation, group['version'], frame, now,
                        facts['cap_artist'], facts['cap_album'], facts['muted'], progress=state, checkpoint=checkpoint)
                elif frame_key in completed:
                    frames[frame] = completed[frame_key]
                else:
                    frames[frame] = candidate_frame(pool, members, facts['blocked'], facts['muted'],
                                                    facts['cap_artist'], facts['cap_album'])
                    completed[frame_key] = frames[frame]
                    if checkpoint is not None:
                        checkpoint(group['version'], frame, -1, {'legacy': frames[frame]})
            prepared[group['group_id']] = {key: group.get(key) for key in
                ('group_id', 'version', 'parent_version', 'name', 'subtitle', 'size', 'leaf', 'display')}
            prepared[group['group_id']]['frames'] = frames
        return {'groups': prepared, 'pools': tuple(pools), 'generation': generation, 'typed': typed}
