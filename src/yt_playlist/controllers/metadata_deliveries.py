"""Capture predecessor metadata response files and commit their SQL references."""
import hashlib
import json
import sqlite3
from uuid import uuid4

from yt_playlist.core.metadata_deliveries import Delivery, NO_RESPONSE
from yt_playlist.core.metadata_delivery_files import FORMAT


class MetadataDeliveries:
    def __init__(self, repository, files):
        self.repository, self.files = repository, files

    def response(self, identity):
        with self.files.retained():
            reference = self.repository.delivery_reference(identity)
            return NO_RESPONSE if reference[0] is None else json.loads(self.files.read_encoded(reference))['value']

    def record(self, delivery):
        delivery.validate()
        encoded = None if delivery.response is NO_RESPONSE else self.files.encode(delivery.response)
        digest = hashlib.sha256(encoded).hexdigest() if encoded is not None else None
        with self.files.retained(), self.repository.transaction():
            previous = self.repository.by_contract(delivery.contract_digest())
            if previous is not None:
                identity = tuple(previous[key] for key in ('provider', 'key_contract', 'delivery_key',
                    'provider_entity_type', 'provider_entity_id', 'input_entry_id'))
                if (identity != delivery.identity() or
                        (previous['input_recording_id'], previous['input_video_id']) !=
                        (delivery.input_recording_id, delivery.input_video_id) or previous['raw_digest'] != digest or
                        encoded is not None and self.files.read_encoded(tuple(previous[key] for key in
                            ('raw_filename', 'raw_digest', 'raw_byte_size', 'raw_offset'))) != encoded):
                    raise sqlite3.IntegrityError('A metadata delivery cannot change its captured evidence')
                return previous['id']
            if delivery.input_entry_id is not None and delivery.key_contract != 'legacy_record':
                subject = self.repository.subject(delivery.input_entry_id)
                if (subject['recording_id'], subject['video_id']) != (delivery.input_recording_id, delivery.input_video_id):
                    raise ValueError('Metadata input subject changed before delivery admission')
            reference = self._response_reference(encoded) if encoded is not None else (None,) * 4
            return self.repository.insert((*delivery.identity()[:5], delivery.input_entry_id, delivery.input_recording_id,
                delivery.input_video_id, delivery.observed_at, delivery.contract_digest(), *reference, FORMAT))

    def _response_reference(self, encoded):
        digest = hashlib.sha256(encoded).hexdigest()
        reference = self.repository.response_reference(digest)
        if reference is not None:
            if self.files.read_encoded(reference) != encoded:
                raise sqlite3.IntegrityError('Metadata response digest collision')
        else:
            reference = self.files.publish(encoded)
            if reference[1] != digest:
                raise sqlite3.IntegrityError('Metadata response changed during publication')
        return reference

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
            reference = self._response_reference(encoded)
            identity = self.repository.insert((*delivery.identity()[:5], entry, subject['recording_id'],
                subject['video_id'], now, delivery.contract_digest(), *reference, FORMAT))
            self.repository.set_capture_head(entry, provider, slot, identity)
            return identity
