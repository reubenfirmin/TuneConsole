"""SQL rows for predecessor metadata deliveries and their selected captures."""
from contextlib import contextmanager

from yt_playlist.core import metadata_locator_contract
from yt_playlist.repos.base import Repo, synchronized


class MetadataDeliveryRepo(Repo):
    @contextmanager
    def transaction(self):
        with self._lock:
            with self.conn.atomic():
                if metadata_locator_contract.retired(self.conn):
                    raise ValueError('Installed delivery writes are retired; use runtime metadata')
                yield

    @synchronized
    def subject(self, entry):
        row = self.conn.execute('SELECT recording_id,video_id,source_ref FROM catalog_entries WHERE id=?', (entry,)).fetchone()
        if row is None:
            raise ValueError('Unknown metadata capture input')
        return dict(row)

    @synchronized
    def capture_head(self, entry, provider, slot):
        row = self.conn.execute('SELECT d.id,d.input_recording_id,d.input_video_id,d.raw_filename,d.raw_digest,'
            'd.raw_byte_size,d.raw_offset FROM metadata_capture_heads h JOIN metadata_deliveries d ON d.id=h.delivery_id '
            'WHERE h.entry_id=? AND h.provider=? AND h.capture_slot=?', (entry, provider, slot)).fetchone()
        return dict(row) if row else None

    @synchronized
    def response_reference(self, digest):
        row = self.conn.execute('SELECT raw_filename,raw_digest,raw_byte_size,raw_offset FROM metadata_deliveries '
                                'WHERE raw_digest=? ORDER BY id LIMIT 1', (digest,)).fetchone()
        return tuple(row) if row else None

    @synchronized
    def delivery_reference(self, identity):
        row = self.conn.execute('SELECT raw_filename,raw_digest,raw_byte_size,raw_offset '
                                'FROM metadata_deliveries WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise ValueError('Unknown metadata delivery')
        return tuple(row)

    @synchronized
    def insert(self, values):
        return self.conn.execute('INSERT INTO metadata_deliveries(provider,key_contract,delivery_key,'
            'provider_entity_type,provider_entity_id,input_entry_id,input_recording_id,input_video_id,'
            'observed_at,contract_digest,raw_filename,raw_digest,raw_byte_size,raw_offset,format_version) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values).lastrowid

    @synchronized
    def set_capture_head(self, entry, provider, slot, identity):
        self.conn.execute('INSERT INTO metadata_capture_heads(entry_id,provider,capture_slot,delivery_id) VALUES (?,?,?,?) '
            'ON CONFLICT(entry_id,provider,capture_slot) DO UPDATE SET delivery_id=excluded.delivery_id',
            (entry, provider, slot, identity))
