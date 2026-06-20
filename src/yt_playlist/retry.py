import logging
import time

logger = logging.getLogger(__name__)

def with_retry(fn, *, attempts=3, base_delay=0.5, sleep=time.sleep):
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # unofficial API: retry transient failures
            last = e
            if i < attempts - 1:
                logger.warning("attempt %d/%d failed: %s", i + 1, attempts, e)
                sleep(base_delay * (2 ** i))
    raise last
