"""Acquire optional related-artist evidence outside model snapshots."""
import logging

from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing, check_stopping
from yt_playlist.rec import artist_model

logger = logging.getLogger(__name__)


def youtube(client, reference):
    if client is None or reference is None:
        return []
    if not (browse := reference['provider_ids'].get('youtube')):
        return []
    check_stopping()
    try:
        return artist_model.youtube_neighbors(client.get_artist(browse))
    except (WorkClosing, WorkBudgetExceeded):
        raise
    except Exception:
        logger.debug('Related artist page unavailable for %s', browse, exc_info=True)
        return []
