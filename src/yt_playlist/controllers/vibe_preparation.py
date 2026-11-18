"""Latest-intent Vibe generation and guarded publication."""
from yt_playlist.core.activity import check_cancellable_work


def publication_phase(result, previous_size):
    phase = 'ready' if len(result.protos) >= 4 and not result.preparing else 'waiting'
    preserve = not result.protos or (phase == 'waiting' and len(result.protos) < previous_size)
    return phase, preserve


class VibePreparation:
    def __init__(self, *, expire, snapshot, capture, remember, atomic, schedule, now, logger):
        self.expire, self.snapshot, self.capture, self.remember = expire, snapshot, capture, remember
        self.atomic, self.schedule, self.now, self.logger = atomic, schedule, now, logger

    def build(self, request, finish):
        now = self.now()
        self.expire(now)
        with self.snapshot() as snapshot:
            captured = self.capture(snapshot.store, request, now)
            if captured is None:
                return
            payload, result, tuning, previous_size = captured
            snapshot.release_reader()
            check_cancellable_work()
            phase, preserve = publication_phase(result, previous_size)
            self.logger.info('Vibe result: request=%s preset=%s phase=%s lanes=%s requested_genres=%s',
                request['id'], request['vibe'], phase, [p['lane'] for p in result.protos], result.requested_genres)
            if preserve:
                with self.atomic():
                    finish(request, phase)
            else:
                self.remember(payload, result, now, tuning=tuning,
                    before_publish=lambda: snapshot.is_current() and finish(request, phase))
        if phase == 'waiting':
            self.schedule()
