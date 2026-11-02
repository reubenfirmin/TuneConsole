"""Provider-scoped delivery identity, separate from shared immutable response bytes."""
from dataclasses import dataclass
import hashlib
import sqlite3
import uuid

from .activity import check_cancellable_work
from .evidence_ledger import canonical
from . import metadata_delivery_files as files, policy_documents

NO_RESPONSE = object()
MAX_BATCH = 128
MAX_BATCH_BYTES = 32 * 1024 * 1024
ENTITY_TYPES = ('recording', 'release', 'artist', 'genre', 'item', 'lookup', 'legacy_unknown')
KEY_CONTRACTS = ('provider_revision', 'capture', 'legacy_record')


@dataclass(frozen=True)
class Delivery:
    provider: str
    delivery_key: str
    provider_entity_type: str
    provider_entity_id: str | None
    observed_at: float | None
    input_entry_id: int | None = None
    input_recording_id: int | None = None
    input_video_id: str | None = None
    key_contract: str = 'capture'
    response: object = NO_RESPONSE

    def identity(self):
        return (self.provider, self.key_contract, self.delivery_key, self.provider_entity_type,
                self.provider_entity_id, self.input_entry_id)

    def contract_digest(self):
        return hashlib.sha256(canonical(['metadata-delivery-identity-1', *self.identity()])).hexdigest()

    def validate(self):
        def text(value, limit):
            return isinstance(value, str) and 0 < len(value.encode()) <= limit
        if (not text(self.provider, 256) or not text(self.delivery_key, 4096)
                or self.provider_entity_type not in ENTITY_TYPES or self.key_contract not in KEY_CONTRACTS
                or (self.provider_entity_id is None and self.provider_entity_type not in ('lookup', 'legacy_unknown'))
                or self.provider_entity_id is not None and not text(self.provider_entity_id, 4096)
                or self.observed_at is None and self.key_contract != 'legacy_record'
                or self.observed_at is not None and (type(self.observed_at) not in (int, float)
                    or not -1.7976931348623157e308 <= self.observed_at <= 1.7976931348623157e308)
                or any(value is not None and (type(value) is not int or not -(1 << 63) <= value < 1 << 63)
                       for value in (self.input_entry_id, self.input_recording_id))
                or self.input_video_id is not None and not text(self.input_video_id, 4096)
                or self.input_entry_id is None and (self.input_recording_id is not None or self.input_video_id is not None)
                or self.input_entry_id is not None and self.input_recording_id is None):
            raise ValueError('Invalid metadata provider delivery contract')


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='metadata_deliveries'").fetchone() is not None


def record_many(conn, deliveries):
    """Keep a failed admission atomic even if its caller handles the exception."""
    if not conn.in_transaction:
        raise RuntimeError('Metadata delivery admission needs an outer transaction')
    from . import metadata_locator_contract as metadata_locators
    if metadata_locators.retired(conn):
        raise ValueError('Installed delivery writes are retired; use runtime metadata')
    conn.execute('SAVEPOINT metadata_delivery_admission')
    try:
        result = _record_many(conn, deliveries)
    except BaseException:
        conn.execute('ROLLBACK TO metadata_delivery_admission')
        raise
    finally:
        conn.execute('RELEASE metadata_delivery_admission')
    return result


def _record_many(conn, deliveries):
    """Admit at most 128 captures atomically inside the caller's transaction.

    The complete bounded batch is checked before files or rows are published.
    The writer reservation also fences file collection. Replays retain the
    first capture time; independent captures retain their own times and owners.
    """
    pending, used = [], 0
    for delivery in deliveries:
        if len(pending) == MAX_BATCH or not isinstance(delivery, Delivery):
            raise ValueError('Metadata delivery batch exceeds its owner budget')
        delivery.validate()
        encoded = None if delivery.response is NO_RESPONSE else files.encode(delivery.response)
        used += len(encoded) if encoded is not None else 0
        if used > MAX_BATCH_BYTES:
            raise ValueError('Metadata delivery batch exceeds its byte budget')
        pending.append((delivery, encoded))
    if not pending:
        return []
    conn.execute('UPDATE metadata_deliveries SET id=id WHERE 0')
    prepared, seen = [], {}
    for delivery, encoded in pending:
        check_cancellable_work()
        digest = delivery.contract_digest()
        raw_digest = hashlib.sha256(encoded).hexdigest() if encoded is not None else None
        evidence = (*delivery.identity(), delivery.input_recording_id, delivery.input_video_id, encoded)
        if digest in seen and seen[digest] != evidence:
            raise sqlite3.IntegrityError('A metadata delivery cannot change its captured evidence')
        seen[digest] = evidence
        existing = conn.execute('SELECT id,provider,key_contract,delivery_key,provider_entity_type,'
            'provider_entity_id,input_entry_id,input_recording_id,input_video_id,raw_filename,raw_digest,'
            'raw_byte_size,raw_offset FROM metadata_deliveries WHERE contract_digest=?', (digest,)).fetchone()
        if existing:
            if (tuple(existing[1:9]) != evidence[:-1] or existing[10] != raw_digest
                    or encoded is not None and files._read(existing[9], existing[10], existing[11], existing[12]) != encoded):
                raise sqlite3.IntegrityError('A metadata delivery cannot change its captured evidence')
            prepared.append((delivery, digest, encoded, existing[0]))
            continue
        if delivery.input_entry_id is not None and delivery.key_contract != 'legacy_record':
            # A retained historical capture can legitimately describe an entry
            # before reassociation. Its recorded FKs stay intact; it must not be
            # rewritten to today's subject merely to pass current admission.
            current = conn.execute('SELECT recording_id,video_id FROM catalog_entries WHERE id=?',
                                   (delivery.input_entry_id,)).fetchone()
            if current is None or tuple(current) != (delivery.input_recording_id, delivery.input_video_id):
                raise ValueError('Metadata input subject changed before delivery admission')
        prepared.append((delivery, digest, encoded, None))
    # New independent deliveries may reuse exact existing content. Digest
    # equality is always followed by a complete byte comparison.
    reused, to_publish = {}, {}
    for _, _, encoded, existing in prepared:
        if existing is not None or encoded is None:
            continue
        digest = hashlib.sha256(encoded).hexdigest()
        if digest in reused or digest in to_publish:
            if digest in to_publish and to_publish[digest] != encoded:
                raise sqlite3.IntegrityError('Metadata response digest collision')
            continue
        row = conn.execute('SELECT raw_filename,raw_digest,raw_byte_size,raw_offset FROM metadata_deliveries '
                           'WHERE raw_digest=? ORDER BY id LIMIT 1', (digest,)).fetchone()
        if row:
            if files._read(*row) != encoded:
                raise sqlite3.IntegrityError('Metadata response digest collision')
            reused[digest] = tuple(row)
        else:
            to_publish[digest] = encoded
    for digest, reference in zip(to_publish, files.publish_encoded(files.directory(conn), to_publish.values())):
        if digest != reference[1]:
            raise sqlite3.IntegrityError('Metadata response changed during publication')
        reused[digest] = reference
    result = []
    for delivery, digest, encoded, existing in prepared:
        check_cancellable_work()
        if existing is None:
            # A duplicate within this same batch now resolves to its first row.
            old = conn.execute('SELECT id FROM metadata_deliveries WHERE contract_digest=?', (digest,)).fetchone()
            if old:
                existing = old[0]
            else:
                reference = reused[hashlib.sha256(encoded).hexdigest()] if encoded is not None else (None,) * 4
                existing = conn.execute('INSERT INTO metadata_deliveries(provider,key_contract,delivery_key,'
                    'provider_entity_type,provider_entity_id,input_entry_id,input_recording_id,input_video_id,'
                    'observed_at,contract_digest,raw_filename,raw_digest,raw_byte_size,raw_offset,format_version) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (*delivery.identity()[:5], delivery.input_entry_id, delivery.input_recording_id,
                     delivery.input_video_id, delivery.observed_at, digest, *reference, files.FORMAT)).lastrowid
        result.append(existing)
    return result


def response(conn, delivery_id):
    """Read under a file fence acquired before pinning this SQL view."""
    with policy_documents.retained_reader(files.directory(conn)):
        conn.execute('SAVEPOINT metadata_delivery_read')
        try:
            row = conn.execute('SELECT raw_filename,raw_digest,raw_byte_size,raw_offset '
                               'FROM metadata_deliveries WHERE id=?', (delivery_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown metadata delivery')
            return NO_RESPONSE if row[0] is None else files.read(*row)
        finally:
            conn.execute('RELEASE metadata_delivery_read')


def capture_state(conn, entry_id, provider, capture_slot, body, *, now):
    """Capture a changed input state where the producer has no delivery key.

    The versioned body describes the actual capture boundary, such as catalog
    ingestion arguments, without asserting that it is the provider's HTTP body.
    A return to an older state is a new capture; unchanged reprocessing reuses
    this source/slot's current capture and does not manufacture another event.
    """
    if not conn.in_transaction:
        raise RuntimeError('Metadata state capture needs an outer transaction')
    if not isinstance(capture_slot, str) or not 0 < len(capture_slot.encode()) <= 256:
        raise ValueError('Invalid metadata state capture slot')
    encoded = files.encode(body)
    conn.execute('SAVEPOINT metadata_state_capture')
    try:
        conn.execute('UPDATE metadata_deliveries SET id=id WHERE 0')
        subject = conn.execute('SELECT recording_id,video_id,source_ref FROM catalog_entries WHERE id=?', (entry_id,)).fetchone()
        if subject is None:
            raise ValueError('Unknown metadata capture input')
        delivery = Delivery(provider, 'state:' + uuid.uuid4().hex, 'item', subject[1] or subject[2], now,
                            entry_id, subject[0], subject[1], response=body)
        delivery.validate()
        head = conn.execute('SELECT d.id,d.input_recording_id,d.input_video_id,d.raw_filename,d.raw_digest,'
            'd.raw_byte_size,d.raw_offset FROM metadata_capture_heads h JOIN metadata_deliveries d ON d.id=h.delivery_id '
            'WHERE h.entry_id=? AND h.provider=? AND h.capture_slot=?', (entry_id, provider, capture_slot)).fetchone()
        if (head and tuple(head[1:3]) == tuple(subject[:2]) and head[3] is not None
                and files._read(*head[3:]) == encoded):
            result = head[0]
        else:
            result, = record_many(conn, [delivery])
            conn.execute('INSERT INTO metadata_capture_heads(entry_id,provider,capture_slot,delivery_id) VALUES (?,?,?,?) '
                         'ON CONFLICT(entry_id,provider,capture_slot) DO UPDATE SET delivery_id=excluded.delivery_id',
                         (entry_id, provider, capture_slot, result))
    except BaseException:
        conn.execute('ROLLBACK TO metadata_state_capture')
        raise
    finally:
        conn.execute('RELEASE metadata_state_capture')
    return result
