import logging
import time

logger = logging.getLogger(__name__)

def error_summary(exc) -> str:
    """Keep warnings readable; ytmusicapi navigation errors embed the whole response."""
    message = str(exc)
    if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str):
        message = exc.args[0]
    if "Unable to find " in message and " using path " in message:
        message = message.split(" on ", 1)[0]
    message = " ".join(message.split())
    if len(message) > 400:
        message = message[:397] + "..."
    return f"{type(exc).__name__}: {message}"


def with_retry(fn, *, attempts=3, base_delay=0.5, sleep=time.sleep, operation=None,
               check_cancelled=None):
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last = None
    for i in range(attempts):
        if check_cancelled is not None:
            check_cancelled()
        try:
            return fn()
        except Exception as e:  # unofficial API: retry transient failures
            if check_cancelled is not None:
                check_cancelled()
            last = e
            context = f"{operation}: " if operation else ""
            logger.debug("%sattempt %d/%d failed", context, i + 1, attempts, exc_info=True)
            if i < attempts - 1:
                delay = base_delay * (2 ** i)
                logger.warning("%sattempt %d/%d failed: %s; retrying in %.1fs",
                               context, i + 1, attempts, error_summary(e), delay)
                sleep(delay)
    raise last
