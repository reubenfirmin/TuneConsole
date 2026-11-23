"""StaticFiles admission/conditional requests with managed original-byte delivery."""
from hashlib import sha256
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles

from yt_playlist.core import fao
from .fao_response import response

HOME_SHELL_STYLES = ('tokens', 'app', 'components', 'tables', 'song-actions', 'genre-controls',
                     'visualization-tooltips', 'playback', 'album-listening', 'onboarding-playback',
                     'storage-maintenance', 'library-controls', 'home', 'onboarding',
                     'library', 'library-tables', 'artwork')
HOME_SHELL_SCRIPTS = ('detail-links', 'vendor/htmx.min', 'theme', 'read-recovery',
                      'playback-status', 'setup-status', 'playback-controls', 'album-player', 'app',
                      'music-search', 'app-navigation', 'listening-choices', 'player-card', 'album-listening',
                      'onboarding-playback', 'song-actions', 'visualization-tooltips', 'table-controls',
                      'track-tables', 'playlist-generation', 'generated-playback', 'home', 'app-update')
HOME_SHELL = {
    'home-shell.css': (HOME_SHELL_STYLES, '.css', b'\n', 'text/css'),
    'home-shell.js': (HOME_SHELL_SCRIPTS, '.js', b'\n;\n', 'text/javascript'),
}


class ManagedStaticFiles(StaticFiles):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._home_shell = {}

    def home_shell(self, path, scope):
        content, etag = self.bundle(path)
        result = Response(content if scope['method'] == 'GET' else b'', media_type=HOME_SHELL[path][3], headers={
            'etag': etag, 'cache-control': 'no-cache', 'content-length': str(len(content))})
        return NotModifiedResponse(result.headers) if self.is_not_modified(result.headers, Headers(scope=scope)) else result

    def bundle(self, path):
        names, extension, separator, _ = HOME_SHELL[path]
        paths = [Path(self.directory) / (name + extension) for name in names]
        identities = tuple((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                           for stat in (path.stat() for path in paths))
        cached = self._home_shell.get(path)
        if cached is None or cached[0] != identities:
            content = separator.join(fao.read(path).bytes for path in paths)
            cached = self._home_shell[path] = identities, content, f'"{sha256(content).hexdigest()}"'
        return cached[1:]

    async def get_response(self, path, scope):
        if path in HOME_SHELL and scope['method'] in ('GET', 'HEAD'):
            return await run_in_threadpool(self.home_shell, path, scope)
        return await super().get_response(path, scope)

    def file_response(self, full_path, stat_result, scope, status_code=200):
        result = response(fao.read(full_path))
        result.status_code = status_code
        if self.is_not_modified(result.headers, Headers(scope=scope)):
            return NotModifiedResponse(result.headers)
        return result
