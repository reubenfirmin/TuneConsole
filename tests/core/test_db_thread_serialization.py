"""A SQLite callback and parameter binding must never freeze the Python GIL."""
import os
import selectors
import subprocess
import sys
import textwrap
import time

import pytest


@pytest.mark.parametrize('factory,operation', [
    (factory, operation)
    for factory in ('Connection', 'DiagnosticConnection')
    for operation in ('execute', 'fetchone', 'fetchmany', 'fetchall', 'iterate')
] + [('Native', 'execute')])
def test_callback_and_concurrent_bind_finish(factory, operation, tmp_path):
    # Keep an actual GIL deadlock out of pytest's own process. Observe where
    # serialization stops the binder, rather than guessing from a short sleep.
    script = textwrap.dedent('''
        import faulthandler, sqlite3, sys, threading
        from yt_playlist.core.db_handles import Connection
        from yt_playlist.core.db_diagnostics import DiagnosticConnection
        native = sys.argv[1] == 'Native'
        factory = sqlite3.Connection if native else globals()[sys.argv[1]]
        operation = sys.argv[2]
        conn = factory(':memory:', check_same_thread=False)
        conn.execute('SELECT ?', (1,)).fetchall()
        cursor = conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
            'SELECT x+1 FROM n WHERE x<1000) SELECT x FROM n')
        entered, contended, adapting, bound = (threading.Event() for _ in range(4))
        failures = []
        def mark(value):
            print(value, flush=True)
        if not native:
            class ObservedLock:
                def __init__(self, lock):
                    self.lock = lock
                def __enter__(self):
                    if not self.lock.acquire(blocking=False):
                        # Observe the binder's first blocked acquisition for
                        # the callback handshake. Execute/fetch may contend
                        # again after the callback has already resumed.
                        if threading.current_thread().name == 'bind' and not contended.is_set():
                            mark('serialized-contention')
                            contended.set()
                        self.lock.acquire()
                    return self
                def __exit__(self, *args):
                    self.lock.release()
            conn._sqlite_lock = ObservedLock(conn._sqlite_lock)
        class Parameter:
            def __conform__(self, protocol):
                # CPython has already prepared/reset the statement and read
                # its parameter metadata here. Its next SQLite call binds the
                # returned integer, retaining the GIL on affected runtimes.
                mark('native-adapter-entered')
                adapting.set()
                assert entered.wait(2)
                mark('native-binding-resuming')
                return 42
        def progress():
            if threading.current_thread().name == 'step' and not entered.is_set():
                mark('callback-entered')
                entered.set()
                if native:
                    # Release the GIL while retaining SQLite's mutex. A safe
                    # native implementation can resume after this bounded
                    # wait; a GIL-holding bind cannot let Python resume at all.
                    bound.wait(.1)
                else:
                    assert contended.wait(2)
                mark('callback-resumed')
            return 0
        conn.set_progress_handler(progress, 1)
        def step():
            try:
                if native:
                    assert adapting.wait(2)
                if operation == 'execute':
                    conn.execute('SELECT sum(x) FROM (SELECT 1 AS x UNION ALL SELECT 2)').fetchone()
                elif operation == 'iterate':
                    next(cursor)
                else:
                    getattr(cursor, operation)()
            except BaseException as error:
                mark('step-failed: ' + repr(error))
                failures.append(error)
        def bind():
            try:
                if not native:
                    assert entered.wait(2)
                parameter = Parameter() if native else 42
                assert conn.execute('SELECT ?', (parameter,)).fetchone() == (42,)
                bound.set()
                mark('binding-finished')
            except BaseException as error:
                mark('bind-failed: ' + repr(error))
                failures.append(error)
        faulthandler.dump_traceback_later(.2 if native else 2)
        threads = [threading.Thread(target=step, name='step'),
                   threading.Thread(target=bind, name='bind')]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        assert not any(thread.is_alive() for thread in threads)
        assert entered.is_set() and bound.is_set() and not failures, failures
        faulthandler.cancel_dump_traceback_later()
        conn.set_progress_handler(None, 0)
        cursor.close()
        conn.close()
    ''')
    command = [sys.executable, '-c', script, factory, operation]
    if factory == 'Native':
        # Stop as soon as the C watchdog establishes the deadlock, rather than
        # spending five seconds waiting for subprocess.run's timeout. Keep a
        # generous failure deadline for a slow child startup; a passing check
        # needs the actual adapter handoff and both complete blocked stacks.
        captured = {'stdout': bytearray(), 'stderr': bytearray()}
        proven = False
        process = subprocess.Popen(command, cwd=tmp_path, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        try:
            with selectors.DefaultSelector() as streams:
                streams.register(process.stdout, selectors.EVENT_READ, 'stdout')
                streams.register(process.stderr, selectors.EVENT_READ, 'stderr')
                deadline = time.monotonic() + 5
                while streams.get_map() and time.monotonic() < deadline:
                    for key, _ in streams.select(.1):
                        chunk = os.read(key.fd, 65536)
                        if chunk:
                            captured[key.data].extend(chunk)
                        else:
                            streams.unregister(key.fileobj)
                    error = captured['stderr']
                    if all(frame in error for frame in
                           (b' in bind\n', b' in progress\n', b' in <module>\n')):
                        proven = True
                        break
        finally:
            if process.poll() is None:
                process.kill()
            stdout_tail, stderr_tail = process.communicate(timeout=2)
            captured['stdout'].extend(stdout_tail)
            captured['stderr'].extend(stderr_tail)
        stdout, stderr = (bytes(captured[name]).decode() for name in ('stdout', 'stderr'))
        diagnostic = f'Native deadlock evidence (exit {process.returncode}):\n{stdout}\n{stderr}'
        assert proven, diagnostic
        assert stdout.splitlines() == [
                'native-adapter-entered', 'callback-entered', 'native-binding-resuming',
        ], diagnostic
        assert ' in __conform__\n' not in stderr, diagnostic
        return
    result = subprocess.run(command,
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    diagnostic = f'Child exited {result.returncode}:\n{result.stdout}\n{result.stderr}'
    assert result.returncode == 0, diagnostic
    assert result.stdout.splitlines() == [
        'callback-entered', 'serialized-contention', 'callback-resumed', 'binding-finished',
    ], diagnostic


def test_callback_can_reenter_connection_and_interrupt_does_not_wait():
    import threading
    import sqlite3
    from yt_playlist.core.db_handles import Connection

    conn = Connection(':memory:', check_same_thread=False)
    entered, release = threading.Event(), threading.Event()
    errors = []

    def callback():
        assert conn.execute('SELECT 42').fetchone() == (42,)
        entered.set()
        assert release.wait(3)
        return 1

    conn.create_function('callback', 0, callback)

    def query():
        try:
            conn.execute('SELECT callback()').fetchall()
        except sqlite3.OperationalError as error:
            errors.append(error)

    thread = threading.Thread(target=query)
    try:
        thread.start()
        assert entered.wait(3)
        conn.interrupt()
    finally:
        release.set()
        thread.join(3)
        conn.close()
    assert not thread.is_alive()
    assert len(errors) == 1 and 'interrupted' in str(errors[0])
