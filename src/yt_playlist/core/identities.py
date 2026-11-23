from yt_playlist.integrations.youtube_music_access import YouTubeMusicAccess
from yt_playlist.integrations.youtube_music_sdk import bridge_client


def build_client(cfg, bridge) -> YouTubeMusicAccess:
    return YouTubeMusicAccess(bridge_client(cfg.brand_account_id, bridge))
