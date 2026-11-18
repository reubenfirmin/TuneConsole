"""Compatibility access to the detached parameter registry and taste commands."""
from yt_playlist.rec.parameter_specs import *
from yt_playlist.rec.parameter_specs import _clamp
from yt_playlist.controllers.taste import Taste


def get_param(store, name):
    return setting_param(store.settings, name)


def setting_param(settings, name):
    PARAMS_BY_NAME[name]
    return parameter_value(name, settings.get_setting(SETTING_PREFIX + name))


def max_track_duration_s(store):
    return 60 * get_param(store, 'max_track_minutes')


def set_param(store, name, value):
    spec = PARAMS_BY_NAME[name]
    store.settings.set_setting(SETTING_PREFIX + name, str(_clamp(spec, value)))


def reset_param(store, name):
    PARAMS_BY_NAME[name]
    store.settings.delete_setting(SETTING_PREFIX + name)


def reset_all_params(store):
    for spec in PARAMS:
        store.settings.delete_setting(SETTING_PREFIX + spec.name)


def checkpoint(store):
    return Taste(store.settings, store.rec_model).checkpoint()


def save_checkpoint(store, now, *, replace=False):
    Taste(store.settings, store.rec_model).save_checkpoint(now, replace=replace)


def restore_checkpoint(store):
    Taste(store.settings, store.rec_model).restore_checkpoint()


def set_weight(store, axis, weight, now):
    Taste(store.settings, store.rec_model).set_weight(axis, weight, now)


def checkpointed_set_param(store, name, value, now):
    Taste(store.settings, store.rec_model).set_param(name, value, now)


def checkpointed_reset_param(store, name, now):
    Taste(store.settings, store.rec_model).reset_param(name, now)


def checkpointed_reset_weights(store, now):
    Taste(store.settings, store.rec_model).reset_weights(now)


def checkpointed_reset_all(store, now):
    Taste(store.settings, store.rec_model).reset_all(now)
