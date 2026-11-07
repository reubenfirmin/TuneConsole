"""Capture predecessor metadata response files and commit their SQL references."""
import hashlib
import sqlite3
from uuid import uuid4

from yt_playlist.core.metadata_deliveries import Delivery
from yt_playlist.core.metadata_delivery_files import FORMAT


class MetadataDeliveries:
    def __init__(self, repository, files):
        self.repository, self.files = repository, files

    def capture_state(self, entry, provider, slot, body, *, now):
        if not isinstance(slot, str) or not 0 < len(slot.encode()) <= 256:
            raise ValueError('Invalid metadata state capture slot')
        encoded = self.files.encode(body)
        with self.files.retained(), self.repository.transaction():
            subject = self.repository.subject(entry)
            delivery = Delivery(provider, 'state:' + uuid4().hex, 'item',
                subject['video_id'] or subject['source_ref'], now, entry, subject['recording_id'],
                subject['video_id'], response=body)
            delivery.validate()
            head = self.repository.capture_head(entry, provider, slot)
            if (head is not None and (head['input_recording_id'], head['input_video_id']) ==
                    (subject['recording_id'], subject['video_id']) and head['raw_filename'] is not None):
                reference = tuple(head[name] for name in ('raw_filename', 'raw_digest', 'raw_byte_size', 'raw_offset'))
                if self.files.read_encoded(reference) == encoded:
                    return head['id']
            digest = hashlib.sha256(encoded).hexdigest()
            reference = self.repository.response_reference(digest)
            if reference is not None:
                if self.files.read_encoded(reference) != encoded:
                    raise sqlite3.IntegrityError('Metadata response digest collision')
            else:
                reference = self.files.publish(encoded)
                if reference[1] != digest:
                    raise sqlite3.IntegrityError('Metadata response changed during publication')
            identity = self.repository.insert((*delivery.identity()[:5], entry, subject['recording_id'],
                subject['video_id'], now, delivery.contract_digest(), *reference, FORMAT))
            self.repository.set_capture_head(entry, provider, slot, identity)
            return identity
