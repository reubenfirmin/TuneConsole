"""Compatibility entry points for Vibe recording preparation."""
from yt_playlist.rec.vibe_recording_rules import *
from yt_playlist.rec.vibe_recording_rules import measured as measured_values, _measured
from yt_playlist.rec import acousticbrainz_vibe_preparation, vibe_joint_model, vibe_joint_preparation


def due(store):
    return (store.vibe_predictions.rebuild_policy() != rebuild_version()
            or store.vibe_predictions.pending() is not None
            or acousticbrainz_vibe_preparation.due(store) or vibe_joint_preparation.due(store))


def step(store, batch=32, *, seconds=10, memory_bytes=vibe_joint_model.MEMORY_BYTES, joint_step=None):
    installed = store.vibe_predictions.ensure_version(rebuild_version())
    dispatched = store.vibe_predictions.fanout(batch)
    waiting = store.prepared_content.has_pending() or store.musical_profiles.pending()
    if acousticbrainz_vibe_preparation.step(store) or acousticbrainz_vibe_preparation.rescore(store, batch):
        return True
    if vibe_joint_preparation.admitted(store, waiting=waiting):
        if (joint_step(waiting=waiting) if joint_step is not None else
                vibe_joint_preparation.step(store, seconds=seconds, memory_bytes=memory_bytes, waiting=waiting)):
            return True
    return dispatched or installed


def measured(inputs, sources, overrides, *, prediction=None):
    if prediction is None and inputs['samples']:
        from yt_playlist.library.vibe_composition import prediction as compose
        prediction = compose().capture()
    return measured_values(inputs, sources, overrides, prediction=prediction)


def recordings(reader, rows, musical, *, source_inputs=None):
    work = recording_steps(reader, rows, musical, source_inputs=source_inputs)
    while True:
        try:
            next(work)
        except StopIteration as completed:
            return completed.value


def recording_steps(reader, rows, musical, *, source_inputs=None):
    from yt_playlist.library.vibe_composition import recording_preparation
    return (yield from recording_preparation(reader).steps(rows, musical, source_inputs=source_inputs))
