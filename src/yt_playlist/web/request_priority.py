"""Give HTTP response preparation priority over cooperative maintenance.

Release at response headers so SSE connections and other long response bodies don't suspend
maintenance for the entire time the browser stays connected. Individual database calls still
take foreground priority when a stream produces its next update. Awaited Activity workers
temporarily release their own request's claim so they cannot yield to their waiting caller.
"""
from urllib.parse import parse_qs

from yt_playlist.core.activity import check_stopping
from yt_playlist.core.lock_diagnostics import database_operation


class RequestPriorityMiddleware:
    def __init__(self, app, priority):
        self.app = app
        self.priority = priority

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # Inherited by synchronous request workers and overridden by named
        # repository operations; omit query strings and form/body values.
        with database_operation('request ' + scope.get('method', '') + ' ' + scope.get('path', '')):
            return await self._http(scope, receive, send)

    async def _http(self, scope, receive, send):
        automatic_home = (scope.get('path') == '/home/growth'
            or (scope.get('path') == '/home/cards'
                and 'prepare_attempt' in parse_qs(
                    scope.get('query_string', b'').decode('latin-1'), keep_blank_values=True))
            or (scope.get('path') == '/'
                and (b'x-home-refresh', b'sync') in scope.get('headers', ())))
        if scope.get('method') == 'GET' and automatic_home:
            # Automatic observation must yield to an explicit listening choice.
            # Keep running the poll with the ordinary cooperative timeslices,
            # read budget and cancellation, rather than suspending its updates.
            with self.priority.background(check_stopping):
                return await self.app(scope, receive, send)
        with self.priority.request() as release:

            async def send_response(message):
                if message["type"] == "http.response.start":
                    release()
                await send(message)

            await self.app(scope, receive, send_response)
