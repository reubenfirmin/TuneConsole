"""Give HTTP response preparation priority over cooperative maintenance.

Release at response headers so SSE connections and other long response bodies don't suspend
maintenance for the entire time the browser stays connected. Individual database calls still
take foreground priority when a stream produces its next update.
"""


class RequestPriorityMiddleware:
    def __init__(self, app, priority):
        self.app = app
        self.priority = priority

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        release = self.priority.enter()

        async def send_response(message):
            if message["type"] == "http.response.start":
                release()
            await send(message)

        try:
            await self.app(scope, receive, send_response)
        finally:
            release()
