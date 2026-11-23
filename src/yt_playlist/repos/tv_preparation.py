"""TV preparation outcome; replacing a display pool cannot erase this state."""
import json

from yt_playlist.core import recommendation_work, worker_settings
from .base import Repo, lookup_read, synchronized


DRAW = recommendation_work.TV_DRAW_KEY


class TVPreparationRepo(Repo):
    @lookup_read
    def state(self):
        return recommendation_work.read(self.conn, 'tv_preparation')

    @synchronized
    def record(self, failed, now):
        with self.conn.atomic():
            recommendation_work.write(self.conn, 'tv_preparation', {'failed': failed, 'at': now}, now)

    @lookup_read
    def draw(self):
        raw = worker_settings.read(self.conn, DRAW)
        if raw is None:
            return 0, [], []
        value = json.loads(raw)
        return value['number'], value['recent'], value['last']

    @synchronized
    def record_draw(self, videos):
        with self.conn.atomic():
            number, history, _ = self.draw()
            videos = list(dict.fromkeys(videos))
            history = [video for video in history if video not in videos]
            recent = recommendation_work.video_history(json.dumps(history + videos), recommendation_work.TV_POOL_LIMIT)
            last = recommendation_work.video_history(json.dumps(videos), recommendation_work.TV_QUEUE_LIMIT)
            worker_settings.save(self.conn, DRAW, json.dumps(dict(number=number + 1, recent=recent, last=last)))
