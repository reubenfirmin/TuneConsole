"""Legacy draft API; detached transitions live in road_trip_rules."""
from yt_playlist.rec.road_trip_rules import *
from yt_playlist.rec.road_trip_rules import (_neighborhoods, _refresh_genre_families,
    _repair_artist_clusters, _pool_targets, _axis_genre, _bucket, _mix_source,
    _quota_weights, _sample_order, _weight)


def add_other_input(state, store, client, item):
    from yt_playlist.library.road_trip_composition import acquisition
    return acquisition(store).add(state, client, item)


def fill_own_facts(state, store, now=0., count=OWN_FACT_LOOKUPS):
    from yt_playlist.library.road_trip_composition import acquisition
    return acquisition(store).fill(state, now, count)


def _commands(store):
    from yt_playlist.library.road_trip_composition import transitions
    return transitions(store)


def repick(state, store, now=0., *, own_pool=None):
    return _commands(store).repick(state, now, own_pool=own_pool)


def reroll_slot(state, store, index, now=0.):
    return _commands(store).reroll(state, index, now)


def start_draft(store, recipe, now, seed, previous=None):
    return _commands(store).start(recipe, now, seed, previous)


def apply_recipe(state, store, now, recipe):
    return _commands(store).apply_recipe(state, recipe, now)


def finish_draft(state, store, now=0.):
    return _commands(store).finish(state, now)


def normalized(state, store=None, now=0.):
    return normalized_values(state) if store is None else _commands(store).normalize(state, now)


def set_share(state, party, axis, share, store, now=0.):
    return _commands(store).tilt(state, party, axis, share, now)


def clear_share(state, party, axis, store, now=0.):
    return _commands(store).unpin(state, party, axis, now)
