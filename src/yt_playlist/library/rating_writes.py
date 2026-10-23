"""Order explicit rating mutations across operations instances sharing a library.

Only queue administration holds the condition. Different accounts/uploads perform their network
writes independently. The owner keeps its turn through retries, local publication and any native
player reconciliation; an HTTP waiter disappearing does not cancel that owner's remote work.
"""
from collections import deque
from contextlib import contextmanager
from threading import Condition
from weakref import WeakKeyDictionary

_condition = Condition()
_queues = WeakKeyDictionary()


@contextmanager
def ordered(store, identity_id, video_id):
    key, token = (identity_id, video_id), object()
    with _condition:
        queues = _queues.setdefault(store, {})
        queue = queues.setdefault(key, deque())
        queue.append(token)
        try:
            _condition.wait_for(lambda: queue[0] is token)
        except BaseException:
            queue.remove(token)
            if not queue:
                del queues[key]
            _condition.notify_all()
            raise
    try:
        yield
    finally:
        with _condition:
            queue.popleft()
            if not queue:
                del queues[key]
            if not queues:
                _queues.pop(store, None)
            _condition.notify_all()
