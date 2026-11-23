"""Capture, stage and publish one admitted mode generation."""


class ModePreparation:
    def __init__(self, *, maintain, resume, snapshot, capture, publish):
        self.maintain, self.resume, self.snapshot, self.capture = maintain, resume, snapshot, capture
        self.publish = publish

    def prepare(self, now):
        self.maintain(now)
        resumed, payload = self.resume()
        if resumed:
            return payload
        with self.snapshot() as snapshot:
            payload, fresh, policies, settings = self.capture(snapshot.store, now)
            published = self.publish(snapshot, payload, fresh, policies, settings, now)
            return payload if published is not None else None
