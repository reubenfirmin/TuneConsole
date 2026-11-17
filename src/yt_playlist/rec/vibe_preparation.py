"""Compatibility entry points for Vibe recording preparation."""
from yt_playlist.rec.vibe_recording_rules import *
from yt_playlist.rec.vibe_recording_rules import measured as measured_values, _measured
from yt_playlist.rec import vibe_joint_model, vibe_joint_preparation


def due(store):
    return (store.settings.get_setting('rec_vibe_version') != version()
            or store.vibe_predictions.pending() is not None or vibe_joint_preparation.due(store))


def step(store, batch=32, *, seconds=10, memory_bytes=vibe_joint_model.MEMORY_BYTES, joint_step=None):
    store.vibe_predictions.ensure_version(version())
    waiting = store.prepared_content.has_pending() or store.musical_profiles.pending()
    if vibe_joint_preparation.admitted(store, waiting=waiting):
        if (joint_step() if joint_step is not None else
                vibe_joint_preparation.step(store, seconds=seconds, memory_bytes=memory_bytes)):
            return True
    return bool(store.vibe_predictions.fanout(batch))


def measured(inputs, sources, selected, overrides, *, prediction=None):
    if prediction is None and inputs['samples']:
        from yt_playlist.library.vibe_composition import prediction as compose
        prediction = compose().capture()
    return measured_values(inputs, sources, selected, overrides, prediction=prediction)


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
