"""Managed file I/O: exact bzip2 bytes, atomic publication and bounded readers.

Domain adapters own logical paths, formats, references and retention decisions.
The physical name is unchanged; no SQL locator, compression suffix or registry
is introduced. Existing plain files remain readable during explicit conversion.
"""
import bz2
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import struct
import tempfile
import time

from .activity import check_cancellable_work


def _try_file_lock(stream, *, exclusive):
    """Return an unlock callback, or None when another handle owns the range."""
    if os.name != 'nt':
        import fcntl
        try:
            fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        return lambda: fcntl.flock(stream, fcntl.LOCK_UN)

    # Native shared locks allow nested reads while fencing orphan collection.
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class Overlapped(ctypes.Structure):
        _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                    ('offset', wintypes.DWORD), ('offset_high', wintypes.DWORD),
                    ('event', wintypes.HANDLE)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped))
    kernel.LockFileEx.restype = wintypes.BOOL
    kernel.UnlockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.POINTER(Overlapped))
    kernel.UnlockFileEx.restype = wintypes.BOOL
    handle, overlapped = msvcrt.get_osfhandle(stream.fileno()), Overlapped()
    if not kernel.LockFileEx(handle, 1 | (2 if exclusive else 0), 0, 1, 0, ctypes.byref(overlapped)):
        error = ctypes.get_last_error()
        if error == 33:  # ERROR_LOCK_VIOLATION
            return None
        raise ctypes.WinError(error)

    def unlock():
        if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
            raise ctypes.WinError(ctypes.get_last_error())
    return unlock


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
class _FileStat:
    logical_bytes: int
    compressed_bytes: int
    allocated_bytes: int
    mtime_ns: int
    ctime_ns: int
    compressed: bool
    digest: str | None
    identity: tuple


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
    return _FileStat(logical, info.st_size, getattr(info, 'st_blocks', (info.st_size + 511)//512)*512,
                    modified, changed, compressed, digest, _identity(info))


def _stat(path):
    with Path(path).open('rb') as stream:
        return _info(stream)


def _exists(path):
    # Existence is independent of validity. Required reads still report damage.
    return Path(path).is_file()


class _Reader:
    def __init__(self, stream, info, max_bytes, work_bytes, required_digest=None):
        if type(max_bytes) is not int or max_bytes < 0 or max_bytes > MAX_BYTES:
            raise ValueError('Invalid managed file byte budget')
        if info.logical_bytes > max_bytes:
            raise FileBudgetExceeded('Managed file exceeds its logical byte budget')
        self.info, self.limit, self.work_limit = info, max_bytes, work_bytes
        self.raw = stream
        self.stream = bz2.BZ2File(stream, 'rb') if info.compressed else stream
        self.position, self.work = 0, 0
        self.digest = hashlib.sha256()
        self.required_digest = required_digest
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
            if self.required_digest is not None and self.digest.hexdigest() != self.required_digest:
                raise CorruptFile('Managed content differs from its required content digest')
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
def _open_reader(path, *, max_bytes=MAX_BYTES, work_bytes=None, digest=None):
    """Pin the inode for streams/leases. Collectors never remove active readers."""
    with Path(path).open('rb') as stream:
        unlock = _try_file_lock(stream, exclusive=False)
        if unlock is None:
            raise FileChanged('Managed file is being collected')
        try:
            reader = _Reader(stream, _info(stream), max_bytes,
                             work_bytes if work_bytes is not None else max_bytes*2, digest)
            try:
                yield reader
            finally:
                if reader.stream is not stream:
                    reader.stream.close()
        finally:
            unlock()


@contextmanager
def _publication(path):
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


def _sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class _PreparedFile:
    """Same-directory bytes prepared before a short guarded publish."""
    def __init__(self, path, pending, expected, durable=False):
        self.path, self.pending, self.expected, self.durable = path, pending, expected, durable
        self.lease = _open_reader(pending)
        self.lease.__enter__()

    def publish(self):
        if self.pending is None:
            raise RuntimeError('Prepared publication has already been consumed')
        with _publication(self.path):
            identity = _identity(self.path.stat()) if _exists(self.path) else False
            if self.expected is not None and identity != self.expected:
                raise FileChanged('Prepared publication observed a replaced source')
            if self.durable:
                # Only recovery files authorizing an external deletion request
                # durable publication, including directories created by callers.
                for parent in reversed(self.path.parent.parents):
                    _sync_directory(parent)
            os.replace(self.pending, self.path)
            self.pending = None
            if self.durable:
                _sync_directory(self.path.parent)
        # The prepared inode remains leased after rename until this context
        # closes. A caller can publish its SQL reference without a deletion gap.

    def close(self):
        if self.lease is not None:
            self.lease.__exit__(None,None,None)
            self.lease = None
        if self.pending is not None:
            self.pending.unlink(missing_ok=True)
            self.pending = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _write(path, chunks, *, max_bytes, age=None, expected=None, expected_digest=None, durable=False):
    """A failed preparation leaves the last published source untouched."""
    path = Path(path)
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_BYTES:
        raise ValueError('Invalid managed file byte budget')
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
            if expected_digest is not None and digest.hexdigest() != expected_digest:
                raise CorruptFile('Managed copy differs from its required content digest')
            output.write(compressor.flush())
            now = time.time_ns()
            modified, changed = (age.mtime_ns, age.ctime_ns) if age is not None else (now, now)
            output.write(TRAILER.pack(MAGIC, size, modified, changed, digest.digest()))
            os.utime(pending, ns=(modified, modified))
            if durable:
                output.flush()
                os.fsync(output.fileno())
        result = _PreparedFile(path, pending, expected, durable)
        pending = None
        return result
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def write(path, content):
    """Compress and atomically publish bytes or the result of read()."""
    with prepare(path, content) as pending:
        if not isinstance(content, Content) or content.path != Path(path):
            # Ordinary writes replace atomically. Only read/modify/write needs
            # a source identity; SQL owners already guard their own revisions.
            pending.expected = None
        pending.publish()
    return Path(path)


def read(path, *, max_bytes=MAX_BYTES, work_bytes=None, digest=None):
    """Return bounded file content; decoding and reader leases are implicit.

    Use .bytes for an entire document, or a with block for streaming. Metadata
    describes the logical file, including the supported installed plain format.
    Passing this content to write/prepare streams a verified copy and preserves
    its timestamps. A write back to the same path also fences replacement.
    """
    return Content(path=Path(path), max_bytes=max_bytes, work_bytes=work_bytes, digest=digest)


def prepare(path, content):
    """Prepare a temporary file; the caller chooses when to publish or close."""
    path = Path(path)
    expected = _identity(path.stat()) if _exists(path) else False
    if isinstance(content, bytes):
        chunks = (memoryview(content)[i:i+BLOCK] for i in range(0,len(content),BLOCK))
        return _write(path, chunks, max_bytes=MAX_BYTES, expected=expected)
    if not isinstance(content, Content):
        raise TypeError('Managed publication requires bytes or file content')
    if content.data is not None:
        chunks = (memoryview(content.data)[i:i+BLOCK] for i in range(0,len(content.data),BLOCK))
        return _write(path, chunks, max_bytes=content.max_bytes, age=content.metadata,
                      expected=content.expected if content.path == path else expected,
                      expected_digest=content.required_digest, durable=content.durable)
    with content as source:
        return _write(path, iter(lambda: source.read(BLOCK), b''), max_bytes=content.max_bytes,
                      age=source.info, expected=source.info.identity if content.path == path else expected,
                      expected_digest=content.required_digest, durable=content.durable)


class Content:
    """A lazy read result, or replacement bytes retaining a read's metadata.

    durable=True is reserved for recovery bytes authorizing external deletion.
    Ordinary cache/evidence content requires no synchronous disk publication.
    """
    def __init__(self, data=None, *, path=None, metadata=None, max_bytes=MAX_BYTES, work_bytes=None,
                 digest=None, durable=False):
        if data is not None and not isinstance(data, bytes):
            raise TypeError('Managed content requires bytes')
        self.path, self.data, self.metadata = path, data, metadata
        self.max_bytes, self.work_bytes = max_bytes, work_bytes
        self.expected = metadata.identity if metadata is not None else None
        self.required_digest = digest
        self.durable = durable

    @property
    def bytes(self):
        if self.data is not None:
            return self.data
        with self as source:
            data = source.read()
        return data

    @property
    def info(self):
        if self.metadata is None:
            self.metadata = _stat(self.path)
        return self.metadata

    @property
    def digest(self):
        digest = hashlib.sha256()
        with self as stream:
            while chunk := stream.read(BLOCK):
                digest.update(chunk)
            return stream.info.logical_bytes, digest.hexdigest()

    def replacing(self, data):
        """Keep the source identity when preparing edited content."""
        return Content(data, path=self.path, metadata=self.info, max_bytes=self.max_bytes, durable=self.durable)

    def __enter__(self):
        self.reader = _open_reader(self.path, max_bytes=self.max_bytes,
                                   work_bytes=self.work_bytes, digest=self.required_digest)
        source = self.reader.__enter__()
        if self.metadata is not None and self.path is not None and source.info.identity != self.metadata.identity:
            self.reader.__exit__(None, None, None)
            raise FileChanged('Managed content observed a replaced source')
        self.metadata = source.info
        self.expected = source.info.identity
        return source

    def __exit__(self, *exc):
        return self.reader.__exit__(*exc)

    def materialize(self):
        return _materialize(self)

    def response(self, **kwargs):
        return _response(self, **kwargs)


def delete(path, *, expected=None, missing_ok=True):
    """Conditional deletion fences stale jobs and active streams/materializations."""
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
                    return True
                finally:
                    unlock()
        except FileNotFoundError:
            if not missing_ok:
                raise
            return False


@contextmanager
def _materialize(content):
    """Bounded real-path lease for third-party decoders and HTTP range serving."""
    with content as source:
        with tempfile.TemporaryDirectory(prefix='tuneconsole-fao-lease-') as directory:
            target = Path(directory) / content.path.name
            with target.open('wb') as output:
                while chunk := source.read(BLOCK):
                    output.write(chunk)
            os.utime(target, ns=(source.info.mtime_ns, source.info.mtime_ns))
            yield target


def _response(content, *, media_type=None, filename=None, headers=None):
    """Deliver original bytes with FileResponse's standard range/cache contract."""
    from starlette.responses import FileResponse
    from starlette.concurrency import run_in_threadpool
    from email.utils import formatdate

    def cache_headers(size,modified):
        return {'content-length':str(size),'last-modified':formatdate(modified//10**9,usegmt=True),
                'etag':'"'+hashlib.md5(f'{modified}:{size}'.encode(),usedforsecurity=False).hexdigest()+'"'}

    path, info = content.path, content.info
    physical = Path(path).stat()
    logical = os.stat_result((physical.st_mode, physical.st_ino, physical.st_dev,
        physical.st_nlink, physical.st_uid, physical.st_gid, info.logical_bytes,
        physical.st_atime, info.mtime_ns / 1e9, info.ctime_ns / 1e9))

    class LeasedResponse(FileResponse):
        async def __call__(self, scope, receive, send):
            lease = _materialize(content)
            materialized = await run_in_threadpool(lease.__enter__)
            try:
                self.path = materialized
                self.stat_result = materialized.stat()
                for name in ('content-length','last-modified','etag'):
                    if name in self.headers:
                        del self.headers[name]
                self.set_stat_headers(self.stat_result)
                self.headers.update(cache_headers(self.stat_result.st_size,self.stat_result.st_mtime_ns))
                # A pathsend server may consume the path after send returns.
                # Stream while this lease is alive instead.
                scope = {**scope, 'extensions': {key: value for key, value in scope.get('extensions', {}).items()
                                                if key != 'http.response.pathsend'}}
                await super().__call__(scope, receive, send)
            finally:
                await run_in_threadpool(lease.__exit__, None, None, None)
    return LeasedResponse(path, media_type=media_type, filename=filename,
                         headers={**(headers or {}),**cache_headers(info.logical_bytes,info.mtime_ns)},stat_result=logical)
