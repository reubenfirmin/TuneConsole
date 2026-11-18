"""Capture, stage and publish one admitted mode generation."""
from yt_playlist.core.proposal_codec import encode_proposals


class ModePreparation:
    def __init__(self, *, maintain, resume, snapshot, capture, typed_publish,
                 proposals, ledger, settings):
        self.maintain, self.resume, self.snapshot, self.capture = maintain, resume, snapshot, capture
        self.typed_publish = typed_publish
        self.proposals, self.ledger, self.settings = proposals, ledger, settings

    def prepare(self, now):
        self.maintain(now)
        resumed, payload = self.resume()
        if resumed:
            return payload
        with self.snapshot() as snapshot:
            payload, fresh, policies, settings = self.capture(snapshot.store, now)
            if self.typed_publish is not None:
                published = self.typed_publish(snapshot, payload, fresh, policies, settings, now)
                return payload if published is not None else None
            encoded = encode_proposals('mode_bundles', payload)
            fresh_encoded = encode_proposals('fresh_pool', fresh)
            snapshot.release_reader()
            prepared = self.proposals.stage_prepared_bundles(encoded, fresh_encoded)

            def publish():
                self.ledger.publish_prepared_policies(list(policies.rows.values()), list(policies.bases.values()))
                self.proposals.publish_prepared_bundles(*prepared, now)
                self.settings.publish_values(settings)

            return payload if snapshot.publish_ordered(publish, validate_inputs=False) else None
