"""Acquire bounded storyboard frames, then publish a detached comparison."""
from yt_playlist.core.activity import WorkBudgetExceeded, WorkClosing


class VisualInspection:
    def __init__(self, *, cached, publish, storyboard, read_image, lock, monotonic):
        self.cached, self.publish, self.storyboard = cached, publish, storyboard
        self.read_image, self.lock, self.monotonic = read_image, lock, monotonic

    def inspect(self, item, now):
        from yt_playlist.rec.storyboard_frames import TIMES, VERSION, RETRY_S, samples, compare
        video = item['video_id']
        if result := self.cached(video, now):
            return result
        if not self.lock.acquire(blocking=False):
            return None
        try:
            if result := self.cached(video, now):
                return result
            result = {'verdict': 'unknown'}
            try:
                selected = samples(self.storyboard(video), video, item['duration']) if item['duration'] > TIMES[-1] else []
                if selected:
                    deadline, sheets, frames = self.monotonic() + 12, {}, []
                    for sample in selected:
                        sheet = sheets.get(sample['url'])
                        if sheet is None:
                            sheet = sheets[sample['url']] = self.read_image(sample['url'], deadline)
                        _, _, right, bottom = sample['box']
                        if right > sheet.width or bottom > sheet.height:
                            raise ValueError('Incomplete timeline sheet')
                        frames.append(sheet.crop(sample['box']))
                    result = {**compare(frames), 'sample_times': [s['at'] for s in selected]}
            except (WorkClosing, WorkBudgetExceeded):
                raise
            except Exception:
                result = {'verdict': 'unknown'}
            result.update(version=VERSION, at=now, retry_at=now + RETRY_S)
            self.publish(video, result, now)
            return result
        finally:
            self.lock.release()
