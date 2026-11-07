"""Own live ASGI servers until their requests and lifespan have finished."""
import threading
import time


class LiveServer:
    """Start a configured Uvicorn server and drain it before releasing its owner.

    A timed join is not a shutdown boundary: requests can still use the parent
    Store and native files afterwards. Uvicorn closes its listeners, drains
    requests, and shuts down the application lifespan before run() returns.
    Waiting for that return also keeps server logging inside pytest's capture
    lifetime. Controlled work in tests must be released before calling close().
    """

    def __init__(self, server, *, sockets=None, startup_timeout=30):
        self.server = server
        self.error = None
        self._finished = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(sockets,), name='test-asgi-server')
        self.thread.start()
        try:
            deadline = time.monotonic() + startup_timeout
            while not server.started:
                if not self.thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError(f'Test server did not start within {startup_timeout} seconds')
                time.sleep(.01)
        except BaseException:
            self.close()
            raise

    def _run(self, sockets):
        try:
            self.server.run(sockets=sockets)
        except BaseException as error:
            self.error = error
        finally:
            self._finished.set()

    def close(self):
        interruption = None
        while True:
            try:
                self.server.should_exit = True
                # A first runtime interrupt can arrive during normal teardown,
                # after pytest has popped this finalizer. Preserve the owner
                # until run() has finished requests, lifespan and logging.
                # Use its completion event rather than trusting an interrupted
                # Thread.join() to retain its thread-liveness bookkeeping.
                self._finished.wait()
                self.thread.join()
                break
            except KeyboardInterrupt as error:
                if interruption is None:
                    interruption = error
        if interruption is not None:
            raise interruption
        if self.error is not None:
            raise RuntimeError('Test server failed') from self.error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
