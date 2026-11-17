"""HTTP delivery of managed content, retaining its lease through the response."""
from email.utils import formatdate
import hashlib
import os
from pathlib import Path

from anyio import CancelScope
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse


def _cache_headers(size, modified):
    return {'content-length': str(size), 'last-modified': formatdate(modified // 10**9, usegmt=True),
            'etag': '"' + hashlib.md5(f'{modified}:{size}'.encode(), usedforsecurity=False).hexdigest() + '"'}


def response(content, *, media_type=None, filename=None, headers=None):
    """Adapt Content to FileResponse's standard range and cache contract."""
    path, info = content.path, content.info
    physical = Path(path).stat()
    logical = os.stat_result((physical.st_mode, physical.st_ino, physical.st_dev,
        physical.st_nlink, physical.st_uid, physical.st_gid, info.logical_bytes,
        physical.st_atime, info.mtime_ns / 1e9, info.ctime_ns / 1e9))

    class LeasedResponse(FileResponse):
        async def __call__(self, scope, receive, send):
            lease = content.materialize()
            with CancelScope(shield=True):
                materialized = await run_in_threadpool(lease.__enter__)
            try:
                self.path = materialized
                self.stat_result = materialized.stat()
                for name in ('content-length', 'last-modified', 'etag'):
                    if name in self.headers:
                        del self.headers[name]
                self.set_stat_headers(self.stat_result)
                self.headers.update(_cache_headers(self.stat_result.st_size, self.stat_result.st_mtime_ns))
                # Servers may consume a pathsend path after send returns. Send
                # bytes here so completion bounds the materialization lifetime.
                scope = {**scope, 'extensions': {key: value for key, value in scope.get('extensions', {}).items()
                                                if key != 'http.response.pathsend'}}
                await super().__call__(scope, receive, send)
            finally:
                with CancelScope(shield=True):
                    await run_in_threadpool(lease.__exit__, None, None, None)

    return LeasedResponse(path, media_type=media_type, filename=filename,
        headers={**(headers or {}), **_cache_headers(info.logical_bytes, info.mtime_ns)}, stat_result=logical)
