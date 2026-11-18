"""Legacy generated-playlist API over acquisition and the shared application command."""
from yt_playlist.library.generated_acquisition import *
from yt_playlist.library.generated_acquisition import _gc_eligible, _gc_eligible_now, _gc_targets


def create_and_record(store, title, tracks, client, now, account_id, *, recipe, **kwargs):
    from yt_playlist.library.generated_composition import commands
    return commands(store, now).create(title, tracks, client=client, account_id=account_id,
                                       recipe=recipe, **kwargs)
