"""Commit a capture ID, write its file, then admit that observation for replay."""
import json
import threading
import uuid

from yt_playlist.core.playback_observation_files import ObservationFiles, encode


class PlaybackInbox:
    def __init__(self, repository, files):
        self.repository, self.files = repository, files
        self.path, self.directory = repository.path, files.root
        self.lock, self.replaying = files.lock, threading.Lock()
        self._recovery_after = 0

    def admission(self):
        return self.files.admission()

    def connect(self):
        return self.repository.connect()

    def _publish(self, capture, kind, body):
        return self.files.publish(capture, kind, body)

    def capture(self, kind, identity, body, now):
        capture = uuid.uuid4().hex
        encoded = encode(capture, kind, body)
        reference = self.files.reference(capture, encoded)
        video = body.get('video_id') if kind == 'rating' else body.get('videoId')
        status = body.get('status') if kind == 'rating' else None
        with self.files.retained():
            with self.admission():
                needs_file = self.repository.reserve(capture, kind, identity, now, video, status, reference,
                    json.dumps(body, allow_nan=False, sort_keys=True, separators=(',', ':')))
            if needs_file:
                if self._publish(capture, kind, body) != reference:
                    raise ValueError('Playback observation differs from its reservation')
                with self.admission():
                    self.repository.complete(capture)
        return capture

    def pending(self):
        with self.admission():
            return self.repository.pending()

    def next(self):
        # A crash can follow a complete file write but precede its SQL status.
        # Reconcile retained bytes; never replay or fabricate the capture itself.
        with self.files.recovery() as admitted, self.admission():
            if admitted:
                page = self.repository.recovery_page(after=self._recovery_after)
                for row in page['pending']:
                    try:
                        self.files.body(row)
                    except (FileNotFoundError, ValueError):
                        continue
                    self.repository.complete(row['capture_id'])
                self._recovery_after = page['after']
            return self.repository.next()

    def rating(self, identity, video):
        with self.admission():
            return self.repository.rating(identity, video)

    def acknowledge(self, capture):
        with self.admission():
            filename = self.repository.acknowledge(capture)
        if filename is not None:
            self.files.delete(filename)

    body = staticmethod(ObservationFiles.body)
