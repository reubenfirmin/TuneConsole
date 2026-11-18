"""Bounded pack construction and verified ranges, independent of SQL and paths.

Owners choose their record codecs, headers and publication lifecycle. Concatenated
records need no delimiter: each reference carries its exact byte offset, size and
SHA-256. Keyed JSON owners can share batching without changing their encoding.
FAO offsets address decompressed bytes; seeking still consumes decompression work.
"""
from dataclasses import dataclass
import hashlib

from . import fao

MAX_ROWS = 128
MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class RecordRange:
    offset: int
    size: int
    digest: str


@dataclass(frozen=True)
class EncodedPack:
    raw: bytes
    records: tuple[RecordRange, ...]


def batches(items, *, size, max_bytes=MAX_BYTES, max_rows=MAX_ROWS,
            overhead=0, target_bytes=None):
    """Yield bounded tuples lazily, allowing one record above the soft target.

    ``size`` includes any per-record framing; ``overhead`` includes the envelope.
    The hard byte limit always includes both. At most one next item is inspected
    before yielding a batch, so publication need not materialize the input stream.
    """
    target_bytes = max_bytes if target_bytes is None else target_bytes
    if (type(max_bytes) is not int or not 0 < max_bytes <= fao.MAX_BYTES
            or type(max_rows) is not int or max_rows < 1
            or type(overhead) is not int or not 0 <= overhead < max_bytes
            or type(target_bytes) is not int or not 0 < target_bytes <= max_bytes):
        raise ValueError('Invalid file pack bounds')
    pending, used = [], overhead
    for item in items:
        length = size(item)
        if type(length) is not int or length < 1:
            raise ValueError('Invalid encoded pack record size')
        if overhead + length > max_bytes:
            raise fao.FileBudgetExceeded('Record exceeds the file pack byte budget')
        if pending and (len(pending) == max_rows or used + length > target_bytes):
            yield tuple(pending)
            pending, used = [], overhead
        pending.append(item)
        used += length
    if pending:
        yield tuple(pending)


def encode(records, *, header, target_bytes, max_bytes=MAX_BYTES, max_rows=MAX_ROWS):
    """Build immutable concatenated packs and their exact record coordinates."""
    if not isinstance(header, bytes):
        raise TypeError('File pack header must be bytes')

    def record_size(raw):
        if not isinstance(raw, bytes):
            raise TypeError('File pack records must be bytes')
        return len(raw)

    for batch in batches(records, size=record_size, max_bytes=max_bytes,
                         max_rows=max_rows, overhead=len(header), target_bytes=target_bytes):
        offset, references = len(header), []
        for raw in batch:
            references.append(RecordRange(offset, len(raw), hashlib.sha256(raw).hexdigest()))
            offset += len(raw)
        yield EncodedPack(header + b''.join(batch), tuple(references))


def validate(raw, rows, *, max_bytes=MAX_BYTES, max_rows=MAX_ROWS):
    """Check an already encoded envelope without prescribing its domain codec."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= max_bytes:
        raise fao.FileBudgetExceeded('File pack exceeds its byte budget')
    if type(rows) is not int or not 0 < rows <= max_rows:
        raise fao.CorruptFile('File pack exceeds its record count budget')


class PackReader:
    """Verify ranges on one caller-owned, pinned FAO stream.

    Adjacent reads reuse the checked header and decompression position. The FAO
    stream continues to enforce its own total decompression work budget.
    """
    def __init__(self, stream, *, header, max_bytes=MAX_BYTES, max_pack_bytes=MAX_BYTES):
        self.stream, self.header = stream, header
        self.max_bytes, self.max_pack_bytes = max_bytes, max_pack_bytes
        self.checked_header = False

    def read(self, offset, size, digest):
        if (type(offset) is not int or offset < 0
                or size is not None and (type(size) is not int or not 0 < size <= self.max_bytes)
                or size is None and offset != 0
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('Invalid file pack record range or digest')
        logical = self.stream.info.logical_bytes
        if logical > self.max_pack_bytes or not offset and logical > self.max_bytes:
            raise fao.FileBudgetExceeded('File pack exceeds its logical byte budget')
        if offset:
            if not self.header or offset < len(self.header) or offset + size > logical:
                raise fao.CorruptFile('Invalid file pack record range')
            if not self.checked_header:
                self.stream.seek(0)
                if self.stream.read(len(self.header)) != self.header:
                    raise fao.CorruptFile('Invalid file pack header')
                self.checked_header = True
        elif size is not None and size != logical:
            raise fao.CorruptFile('File pack record does not match its size')
        self.stream.seek(offset)
        raw = self.stream.read(size if size is not None else self.max_bytes + 1)
        if (len(raw) > self.max_bytes or size is not None and len(raw) != size
                or hashlib.sha256(raw).hexdigest() != digest):
            raise fao.CorruptFile('File pack record does not match its digest or size')
        return raw
