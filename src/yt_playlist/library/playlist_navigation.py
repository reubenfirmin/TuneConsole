"""Wait for a newly created playlist to propagate before switching the YouTube Music tab."""
import logging

logger = logging.getLogger(__name__)


def _playlist_ready(client, playlist_id):
    try:
        return bool((client.get_playlist(playlist_id, limit=1) or {}).get("tracks"))
    except Exception:
        return False


def navigate_when_ready(client, bridge, ytm_id, watch_url, *, tries=20, interval_s=1.5,
                        sleep=None):
    """Poll on a background thread; leave the current tab alone when propagation times out."""
    import time
    sleep = sleep or time.sleep
    for index in range(tries):
        if _playlist_ready(client, ytm_id):
            bridge.send_control({"type": "navigate", "url": watch_url})
            return True
        if index < tries - 1:
            sleep(interval_s)
    logger.warning("playlist %s not confirmed ready after ~%.0fs; skipping the YTM switch",
                   ytm_id, tries * interval_s)
    return False
