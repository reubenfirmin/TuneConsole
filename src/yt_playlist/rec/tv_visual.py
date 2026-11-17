"""Compatibility acquisition for bounded video storyboard inspection."""
from io import BytesIO
import threading
import time
from PIL import Image

from yt_playlist.integrations.youtube_music import storyboards
from yt_playlist.controllers.visual_inspection import VisualInspection
from yt_playlist.rec.storyboard_frames import SURFACE, VERSION, TIMES, LIMIT, RETRY_S, MAX_PIXELS, samples, compare


def cached(store, video_id, now):
    value = (store.rec_proposals.get_proposals(SURFACE) or {}).get(video_id)
    if (isinstance(value, dict) and value.get('version') == VERSION
            and value.get('verdict') in {'static', 'moving', 'unknown'}
            and (value['verdict'] != 'unknown' or now < value.get('retry_at', 0))):
        return value
    return None


def static_ids(store):
    return store.tv.static_ids(VERSION)


def _read_image(url, deadline):
    image = Image.open(BytesIO(storyboards.read(url, deadline)))
    if image.width * image.height > MAX_PIXELS:
        raise ValueError('Timeline sheet is too large')
    image.load()
    return image


class Inspector:
    def __init__(self):
        self.lock = threading.Lock()

    def inspect(self, store, client, item, now):
        return VisualInspection(cached=lambda video, at: cached(store, video, at),
            publish=store.tv.publish_visual, storyboard=getattr(client, 'get_video_storyboard', lambda video: {}),
            read_image=_read_image, lock=self.lock, monotonic=time.monotonic).inspect(item, now)
