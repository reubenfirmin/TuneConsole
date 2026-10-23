"""Keep numerical maintenance from spawning a CPU pool alongside the UI.

Limits deliberately stay at one: restoring a per-call context could reset a process-wide pool
while another thread is still using it. Reapply in each worker for thread-local BLAS/OpenMP APIs.
"""
from functools import lru_cache

from threadpoolctl import ThreadpoolController


@lru_cache(maxsize=1)
def _controller():
    # Load both runtimes before discovering their native libraries. A controller only knows the
    # libraries that were loaded when it was created; scipy wheels can carry a separate BLAS.
    import numpy  # noqa: F401
    import scipy.linalg  # noqa: F401
    return ThreadpoolController()


def limit_native_threads():
    _controller().limit(limits=1)
