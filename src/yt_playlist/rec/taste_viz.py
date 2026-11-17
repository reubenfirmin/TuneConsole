"""Compatibility entry points; Taste commands capture inputs for detached display models."""
from yt_playlist.library import listening_composition as layers
from yt_playlist.library.taste_reading_composition import read
from yt_playlist.rec import embedding_vectors as vectors, scoring, transient
from yt_playlist.rec.taste_display import _axis_rows, _graduation_change, _attach_graduation, _breadth_word, _ribbon_segments, _permanent_mode_shares


def mix_choices(store, now=None):
    return read(store, 'mix_choices', now)


def generated_playlists(store, now):
    return read(store, 'generated_playlists', now)


def _stored_examples(store, now):
    return read(store, '_stored_examples', now)


def source_weights(store, now):
    return read(store, 'source_weights', now)


def overview(store, now, profiles=None):
    return read(store, 'overview', now, profiles=profiles)


def _artist_shares(store, top=12):
    return read(store, '_artist_shares', top=top)


def mode_layers(store, now):
    return read(store, 'mode_layers', now)


def _sources(store, now):
    return read(store, '_sources', now)


def learning_progress(store, now):
    return read(store, 'learning_progress', now)


def model_transparency(store, now):
    return read(store, 'model_transparency', now)


def engine_panel(store):
    return read(store, 'engine_panel')


def playlist_influence_panel(store, top=8):
    return read(store, 'playlist_influence_panel', top=top)


def centroid_tilt_panel(store, now, top=6):
    return read(store, 'centroid_tilt_panel', now, top=top)
