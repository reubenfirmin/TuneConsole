"""Playback observations and maintenance state for isolated feedback checks."""
from contextlib import contextmanager


@contextmanager
def paused_recording(store):
    previous = getattr(store, '_recommendation_retirement_pending', False)
    store._recommendation_retirement_pending = True
    try:
        yield
    finally:
        store._recommendation_retirement_pending = previous


def event(position):
    return {'kind': 'next', 'videoId': 'song', 'position': position, 'duration': 180}
