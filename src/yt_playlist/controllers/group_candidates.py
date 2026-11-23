"""Resume bounded candidate frames separately from report-based allocation."""
from yt_playlist.core.activity import check_stopping
from yt_playlist.rec.stored_groups import PreviewFrame


class GroupCandidates:
    def __init__(self, *, capture, frame):
        self.capture, self.frame = capture, frame

    def prepare(self, head, now, *, progress=None):
        progress = progress if progress is not None else {}
        completed = progress.setdefault('frames', {})
        facts = self.capture()
        generation, pools = facts['generation'], facts['pools']
        prepared = {}
        for group in head['groups']:
            if group.get('legacy'):
                continue
            check_stopping()
            frames = {}
            for frame in pools:
                frame_key = (group['version'], frame)
                state = completed.setdefault(frame_key, PreviewFrame())
                frames[frame] = self.frame(generation, group['version'], frame, now,
                    facts['cap_artist'], facts['cap_album'], facts['muted'], progress=state)
            prepared[group['group_id']] = {key: group.get(key) for key in
                ('group_id', 'version', 'parent_version', 'name', 'subtitle', 'size', 'leaf', 'display')}
            prepared[group['group_id']]['frames'] = frames
        return {'groups': prepared, 'pools': pools, 'generation': generation}
