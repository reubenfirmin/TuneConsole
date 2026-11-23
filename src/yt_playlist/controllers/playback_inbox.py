"""Capture, inspect and acknowledge the ordered playback queue."""
import threading
import uuid

from yt_playlist.core.playback_inbox_file import validate_capture


class PlaybackInbox:
    def __init__(self, file):
        self.file = file
        self.path = file.path
        self.replaying = threading.Lock()

    def capture(self, kind, identity, body, now):
        capture = uuid.uuid4().hex
        row = dict(capture_id=capture, kind=kind, account_id=identity, observed_at=now, body=body,
                   video_id=body.get('video_id' if kind == 'rating' else 'videoId'),
                   rating_status=body.get('status') if kind == 'rating' else None)
        validate_capture(row)
        with self.file.admission():
            queue = self.file.read()
            queue['sequence'] += 1
            row['sequence'] = queue['sequence']
            queue['captures'].append(row)
            self.file.write(queue)
        return capture

    def pending(self):
        return len(self.file.read()['captures'])

    def next(self):
        rows = self.file.read()['captures']
        return rows[0] if rows else None

    def rating(self, identity, video):
        for row in reversed(self.file.read()['captures']):
            if row['kind'] == 'rating' and row['account_id'] == identity and row['video_id'] == video:
                return {'status': row['rating_status'], 'observed_at': row['observed_at'], 'source': 'api'}
        return None

    def acknowledge(self, capture):
        with self.file.admission():
            queue = self.file.read()
            rows = queue['captures']
            # Another process may have already acknowledged the same head.
            if rows and rows[0]['capture_id'] == capture:
                del rows[0]
                self.file.write(queue)
