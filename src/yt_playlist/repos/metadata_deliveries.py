"""SQL rows for predecessor metadata deliveries and their selected captures."""
from contextlib import contextmanager

from yt_playlist.core import metadata_deliveries
from yt_playlist.repos.base import Repo, synchronized
from yt_playlist.repos import delivery_file_writes


class MetadataDeliveryRepo(Repo):
    def _require_capture(self):
        if not self.conn.execute('SELECT allowed FROM metadata_delivery_capture_allowed').fetchone()[0]:
            raise ValueError('Installed delivery writes are retired; use runtime metadata')

    @contextmanager
    def transaction(self):
        with self._lock:
            with self.conn.atomic():
                self._require_capture()
                yield

    @synchronized
    def reserve_response(self, contract, digest, size):
        if self.conn.in_transaction:
            raise ValueError('Metadata response reservation requires its own transaction')
        with self.conn.atomic():
            self._require_capture()
            return delivery_file_writes.reserve(self.conn, contract, digest, size)

    @synchronized
    def complete_response(self, identity, contract, delivery, reference):
        delivery_file_writes.complete(self.conn, identity, contract, delivery, reference)

    @synchronized
    def subject(self, entry):
        row = self.conn.execute('SELECT recording_id,video_id,source_ref FROM track_sources WHERE id=?', (entry,)).fetchone()
        if row is None:
            raise ValueError('Unknown metadata capture input')
        return dict(row)

    @synchronized
    def capture_head(self, entry, provider, slot):
        row = metadata_deliveries.capture_head(self.conn, entry, provider, slot)
        return dict(row) if row else None

    @synchronized
    def response_reference(self, digest):
        row = self.conn.execute('SELECT raw_filename,raw_digest,raw_byte_size,raw_offset FROM metadata_delivery_locations '
                                'WHERE raw_digest=? ORDER BY id LIMIT 1', (digest,)).fetchone()
        return tuple(row) if row else None

    @synchronized
    def delivery_reference(self, identity):
        row = self.conn.execute('SELECT raw_filename,raw_digest,raw_byte_size,raw_offset '
                                'FROM metadata_delivery_locations WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise ValueError('Unknown metadata delivery')
        return tuple(row)

    @synchronized
    def by_contract(self, digest):
        row = self.conn.execute('SELECT d.id,provider,key_contract,delivery_key,provider_entity_type,provider_entity_id,'
            'input_entry_id,input_recording_id,input_video_id,l.raw_filename,l.raw_digest,l.raw_byte_size,l.raw_offset '
            'FROM metadata_delivery_locations l JOIN metadata_deliveries d ON d.id=l.id WHERE l.contract_digest=?',
            (digest,)).fetchone()
        return dict(row) if row else None

    @synchronized
    def insert(self, values):
        return self.conn.execute('INSERT INTO metadata_deliveries(provider,key_contract,delivery_key,'
            'provider_entity_type,provider_entity_id,input_entry_id,input_recording_id,input_video_id,'
            'observed_at,contract_digest,raw_filename,raw_digest,raw_byte_size,raw_offset,format_version) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values).lastrowid

    @synchronized
    def set_capture_head(self, entry, provider, slot, identity):
        metadata_deliveries.set_capture_head(self.conn, entry, provider, slot, identity)
