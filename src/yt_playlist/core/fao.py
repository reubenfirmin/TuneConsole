"""Managed file I/O: exact bzip2 bytes, atomic publication and bounded readers.

Domain adapters own logical paths, formats, references and retention decisions.
The physical name is unchanged; no SQL locator, compression suffix or registry
is introduced. Existing plain files remain readable during explicit conversion.
"""
import bz2
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import time

from .activity import check_cancellable_work

BLOCK = 64 * 1024
MAX_BYTES = 512 * 1024 * 1024
# A trailing custody record does not alter bz2.decompress's original output.
# In particular Linux inode ctime is captured before replacement, never guessed
# from the new inode. Backup copies carry this record with the compressed bytes.
TRAILER = struct.Struct('>16sQQQ32s')
MAGIC = b'TuneConsole FAO1'
MAGIC = MAGIC.ljust(16, b'\0')


class CorruptFile(ValueError):
    """Required content exists but fails its encoding/integrity contract."""


class FileBudgetExceeded(ValueError):
    """An operation exceeded the caller's explicit logical/work budget."""


class FileChanged(RuntimeError):
    """A stale conversion/collector observed a different publication."""


@dataclass(frozen=True)
class FileStat:
    logical_bytes: int
    compressed_bytes: int
    allocated_bytes: int
    mtime_ns: int
    ctime_ns: int
    compressed: bool
    digest: str | None
    identity: tuple


@dataclass(frozen=True)
class Retention:
    days: int
    clock: str

    def __post_init__(self):
        if self.days not in (7, 30, 180) or self.clock not in ('ctime', 'mtime'):
            raise ValueError('Retention requires 7, 30 or 180 days and ctime or mtime')

    def eligible(self, info, now):
        age = info.ctime_ns if self.clock == 'ctime' else info.mtime_ns
        return age / 1e9 <= now - self.days * 86400


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _info(stream):
    info = os.fstat(stream.fileno())
    stream.seek(0)
    compressed = stream.read(3) == b'BZh'
    logical, modified, changed, digest = info.st_size, info.st_mtime_ns, info.st_ctime_ns, None
    if compressed:
        if info.st_size < TRAILER.size:
            raise CorruptFile('Compressed managed file has no custody record')
        stream.seek(-TRAILER.size, os.SEEK_END)
        magic, logical, modified, changed, digest = TRAILER.unpack(stream.read(TRAILER.size))
        if magic != MAGIC:
            raise CorruptFile('Unsupported compressed managed file')
        digest = digest.hex()
    stream.seek(0)
    return FileStat(logical, info.st_size, getattr(info, 'st_blocks', (info.st_size + 511)//512)*512,
                    modified, changed, compressed, digest, _identity(info))


def stat(path):
    with Path(path).open('rb') as stream:
        return _info(stream)


def exists(path):
    # Existence is independent of validity. Required reads still report damage.
    return Path(path).is_file()


def listing(root, pattern='**/*'):
    """Logical files only, including both installed and converted formats."""
    return (p for p in Path(root).glob(pattern) if p.is_file() and not p.name.startswith('.'))


class Reader:
    def __init__(self, stream, info, max_bytes, work_bytes):
        if type(max_bytes) is not int or max_bytes < 0 or max_bytes > MAX_BYTES:
            raise ValueError('Invalid managed file byte budget')
        if info.logical_bytes > max_bytes:
            raise FileBudgetExceeded('Managed file exceeds its logical byte budget')
        self.info, self.limit, self.work_limit = info, max_bytes, work_bytes
        self.raw = stream
        self.stream = bz2.BZ2File(stream, 'rb') if info.compressed else stream
        self.position, self.work = 0, 0
        self.digest = hashlib.sha256()
        self.verified = False

    def read(self, size=-1):
        check_cancellable_work()
        if size == 0:
            return b''
        if size is None or size < 0:
            size = self.info.logical_bytes - self.position + 1
        size = min(size, self.limit - self.position + 1)
        if self.work + min(size, self.info.logical_bytes - self.position) > self.work_limit:
            raise FileBudgetExceeded('Managed read exceeds its decompression work budget')
        try:
            data = self.stream.read(size)
        except (OSError, EOFError) as exc:
            raise CorruptFile('Managed file cannot be decompressed or verified against its digest') from exc
        self.work += len(data)
        self.position += len(data)
        self.digest.update(data)
        if self.position > self.info.logical_bytes:
            raise CorruptFile('Managed file differs from its logical byte size')
        if not data or self.position == self.info.logical_bytes:
            if self.position != self.info.logical_bytes:
                raise CorruptFile('Managed file is truncated')
            if self.info.digest and self.digest.hexdigest() != self.info.digest:
                raise CorruptFile('Managed file failed integrity verification')
            # Require end of stream as well as the declared output length.
            try:
                if self.stream.read(1):
                    raise CorruptFile('Managed file exceeds its declared byte size')
            except (OSError, EOFError) as exc:
                raise CorruptFile('Managed file is truncated') from exc
            self.verified = True
        return data

    def seek(self, offset, whence=os.SEEK_SET):
        target = offset if whence == os.SEEK_SET else self.position + offset if whence == os.SEEK_CUR else self.info.logical_bytes + offset
        if not 0 <= target <= self.info.logical_bytes:
            raise FileBudgetExceeded('Managed seek exceeds its logical byte budget')
        if target < self.position:
            self.stream.seek(0)
            self.position, self.digest, self.verified = 0, hashlib.sha256(), False
        while self.position < target:
            self.read(min(BLOCK, target-self.position))
        return target

    def tell(self):
        return self.position


@contextmanager
def open_reader(path, *, max_bytes=MAX_BYTES, work_bytes=None):
    """Pin the inode for streams/leases. Collectors never remove active readers."""
    from .policy_documents import _try_file_lock
    with Path(path).open('rb') as stream:
        unlock = _try_file_lock(stream, exclusive=False)
        if unlock is None:
            raise FileChanged('Managed file is being collected')
        try:
            reader = Reader(stream, _info(stream), max_bytes, work_bytes if work_bytes is not None else max_bytes*2)
            try:
                yield reader
            finally:
                if reader.stream is not stream:
                    reader.stream.close()
        finally:
            unlock()


def read_bytes(path, *, max_bytes=MAX_BYTES):
    with open_reader(path, max_bytes=max_bytes) as stream:
        return stream.read()


def read_text(path, *, max_bytes=MAX_BYTES, encoding='utf-8'):
    return read_bytes(path, max_bytes=max_bytes).decode(encoding)


def _sync(directory):
    from .evidence_ledger import _sync_directory
    _sync_directory(directory)


@contextmanager
def _publication(path):
    from .policy_documents import _try_file_lock
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / '.fao.lock').open('a+b') as lock:
        while True:
            check_cancellable_work()
            unlock = _try_file_lock(lock, exclusive=True)
            if unlock:
                break
            time.sleep(.01)
        try:
            yield
        finally:
            unlock()


def _write(path, chunks, *, max_bytes, age=None, expected=None, immutable=False):
    """A failed write/verification leaves the last published source untouched."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.fao-pending-', delete=False) as output:
            pending = Path(output.name)
            compressor, digest, size = bz2.BZ2Compressor(9), hashlib.sha256(), 0
            for chunk in chunks:
                check_cancellable_work()
                size += len(chunk)
                if size > max_bytes:
                    raise FileBudgetExceeded('Managed publication exceeds its logical byte budget')
                digest.update(chunk)
                output.write(compressor.compress(chunk))
            output.write(compressor.flush())
            now = time.time_ns()
            modified, changed = (age.mtime_ns, age.ctime_ns) if age is not None else (now, now)
            output.write(TRAILER.pack(MAGIC, size, modified, changed, digest.digest()))
            output.flush()
            os.fsync(output.fileno())
        # Verify in bounded blocks before atomic publication, including ENOSPC
        # and malformed compressor output. No second full uncompressed copy.
        with open_reader(pending, max_bytes=max_bytes) as verify:
            while verify.read(BLOCK):
                pass
        os.utime(pending, ns=(modified, modified))
        with _publication(path):
            if expected is not None and (not exists(path) or stat(path).identity != expected):
                raise FileChanged('Managed publication replaced a stale source')
            if immutable and exists(path):
                current = stat(path)
                if current.logical_bytes != size:
                    raise CorruptFile('Immutable managed publication differs from its content digest')
                with open_reader(path, max_bytes=max_bytes) as reader:
                    found = hashlib.sha256()
                    while chunk := reader.read(BLOCK):
                        found.update(chunk)
                if found.digest() != digest.digest():
                    raise CorruptFile('Immutable managed publication differs from its content digest')
                return path
            os.replace(pending, path)
            pending = None
            _sync(path.parent)
        return path
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def write_bytes(path, data, *, immutable=False, max_bytes=MAX_BYTES):
    if not isinstance(data, bytes):
        raise TypeError('Managed publication requires bytes')
    return _write(path, (memoryview(data)[i:i+BLOCK] for i in range(0, len(data), BLOCK)),
                  max_bytes=max_bytes, immutable=immutable)


def write_stream(path, chunks, *, max_bytes=MAX_BYTES):
    return _write(path, chunks, max_bytes=max_bytes)


def convert(path, *, max_bytes=MAX_BYTES):
    """One restartable explicit preparation unit; never called at startup."""
    with open_reader(path, max_bytes=max_bytes) as source:
        if source.info.compressed:
            return False
        _write(path, iter(lambda: source.read(BLOCK), b''), max_bytes=max_bytes,
               age=source.info, expected=source.info.identity)
        return True


def copy(source, target, *, max_bytes=MAX_BYTES):
    """Verified backup/restore/relocation preserving both declared age clocks."""
    with open_reader(source, max_bytes=max_bytes) as reader:
        return _write(target, iter(lambda: reader.read(BLOCK), b''), max_bytes=max_bytes, age=reader.info)


def delete(path, *, expected=None, missing_ok=False):
    """Conditional deletion fences stale jobs and active streams/materializations."""
    from .policy_documents import _try_file_lock
    path = Path(path)
    with _publication(path):
        try:
            with path.open('rb') as stream:
                if expected is not None and _identity(os.fstat(stream.fileno())) != expected:
                    return False
                unlock = _try_file_lock(stream, exclusive=True)
                if unlock is None:
                    return False
                try:
                    path.unlink()
                    _sync(path.parent)
                    return True
                finally:
                    unlock()
        except FileNotFoundError:
            if not missing_ok:
                raise
            return False


def expire(path, policy, *, now, required, expired):
    """Owner supplies a fenced reference check and records intentional absence.

    Caller holds its domain's publication/SQL reservation throughout. Persist
    expiry intent before removal: restart may finish removal, never refetch.
    """
    info = stat(path)
    if required() or not policy.eligible(info, now):
        return False
    expired()
    return delete(path, expected=info.identity, missing_ok=True)


@contextmanager
def materialize(path, *, max_bytes=MAX_BYTES):
    """Bounded real-path lease for third-party decoders and HTTP range serving."""
    with open_reader(path, max_bytes=max_bytes) as source:
        with tempfile.TemporaryDirectory(prefix='tuneconsole-fao-lease-') as directory:
            target = Path(directory) / Path(path).name
            with target.open('wb') as output:
                while chunk := source.read(BLOCK):
                    output.write(chunk)
            os.utime(target, ns=(source.info.mtime_ns, source.info.mtime_ns))
            yield target


def root_for_database(path, memory_key, domain):
    if path:
        path = Path(path).resolve()
        return path.with_name(path.name + '.' + domain)
    return Path(tempfile.gettempdir()) / ('tuneconsole-' + domain) / str(memory_key)


def directory(conn, domain):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    return root_for_database(path, getattr(conn, '_policy_memory_key', id(conn)), domain)


@contextmanager
def collector(root):
    """Try the existing domain snapshot fence; a busy owner is retried later."""
    from .policy_documents import _try_file_lock
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.readers.lock').open('a+b') as stream:
        unlock = _try_file_lock(stream, exclusive=True)
        try:
            yield unlock is not None
        finally:
            if unlock is not None:
                unlock()


def _hash(path):
    digest = hashlib.sha256()
    with open_reader(path) as stream:
        while chunk := stream.read(BLOCK):
            digest.update(chunk)
        return stream.info.logical_bytes, digest.hexdigest()


def export(root, paths, target):
    """Backup an explicit domain inventory, never discover owners implicitly."""
    root, target = Path(root).resolve(), Path(target).resolve()
    if root == target:
        raise ValueError('Managed backup needs a separate destination')
    entries = []
    for source in sorted(set(map(Path, paths))):
        relative = source.resolve().relative_to(root)
        size, digest = _hash(source)
        copy(source, target / relative)
        entries.append([relative.as_posix(), size, digest])
    manifest = target / 'manifest.json'
    write_bytes(manifest, json.dumps({'format': 'fao-backup-1', 'files': entries}, separators=(',', ':')).encode())
    return str(manifest)


def restore(root, manifest, *, expected):
    root, manifest = Path(root).resolve(), Path(manifest)
    document = json.loads(read_bytes(manifest))
    if not isinstance(document, dict) or set(document) != {'format', 'files'} or document['format'] != 'fao-backup-1':
        raise CorruptFile('Unsupported managed backup')
    required = {Path(path).resolve().relative_to(root).as_posix() for path in expected}
    admitted = {}
    for name, size, digest in document['files']:
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or name not in required or name in admitted:
            raise CorruptFile('Managed backup differs from its domain owners')
        source = manifest.parent / relative
        if _hash(source) != (size, digest):
            raise CorruptFile('Managed backup differs from its content digest')
        admitted[name] = source
    if admitted.keys() != required:
        raise CorruptFile('Managed backup lacks required domain files')
    for name, source in admitted.items():
        copy(source, root / name)


def response(path, *, media_type=None, filename=None, headers=None, max_bytes=MAX_BYTES):
    """Deliver original bytes with FileResponse's standard range/cache contract."""
    from starlette.responses import FileResponse
    from starlette.concurrency import run_in_threadpool

    class LeasedResponse(FileResponse):
        async def __call__(self, scope, receive, send):
            lease = materialize(path, max_bytes=max_bytes)
            materialized = await run_in_threadpool(lease.__enter__)
            try:
                self.path = materialized
                await super().__call__(scope, receive, send)
            finally:
                await run_in_threadpool(lease.__exit__, None, None, None)
    return LeasedResponse(path, media_type=media_type, filename=filename, headers=headers)
