"""Compatibility entry points for bounded joint Vibe preparation."""
from yt_playlist.rec.vibe_joint_state import *


def step(store, *, seconds=10, memory_bytes=model.MEMORY_BYTES, waiting=False):
    work = steps(store, seconds=seconds, memory_bytes=memory_bytes, waiting=waiting)
    try:
        while True:
            next(work)
    except StopIteration as completed:
        return completed.value
    finally:
        work.close()


def steps(store, *, seconds=10, input_seconds=2, memory_bytes=model.MEMORY_BYTES, waiting=False):
    from yt_playlist.library.vibe_composition import joint_preparation
    return (yield from joint_preparation(store).steps(memory_bytes, waiting=waiting))
