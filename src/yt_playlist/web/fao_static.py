"""StaticFiles admission/conditional requests with managed original-byte delivery."""
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse, StaticFiles

from yt_playlist.core import fao


class ManagedStaticFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = fao.read(full_path).response()
        response.status_code = status_code
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response
